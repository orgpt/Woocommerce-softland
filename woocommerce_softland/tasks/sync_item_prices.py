from typing import List, Optional

import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPrice
from frappe import qb
from frappe.query_builder import Criterion

from woocommerce_softland.tasks.sync import SynchroniseWooCommerce
from woocommerce_softland.woocommerce.doctype.woocommerce_server.woocommerce_server import (
    WooCommerceServer,
)
from woocommerce_softland.woocommerce.woocommerce_api import (
    generate_woocommerce_record_name_from_domain_and_id,
)

BATCH_SIZE = 50 


# ------------------------------------------------------------
# Hooks 
# ------------------------------------------------------------

def update_item_price_for_woocommerce_item_from_hook(doc, method):
    if not frappe.flags.in_test:
        if doc.doctype == "Item Price":
            frappe.enqueue(
                "woocommerce_softland.tasks.sync_item_prices.run_item_price_sync",
                enqueue_after_commit=True,
                queue="long",
                timeout=600,
                item_code=doc.item_code,
                item_price_doc=doc,
            )


# ------------------------------------------------------------
# Public API 
# ------------------------------------------------------------

@frappe.whitelist()
def run_item_price_sync_in_background():
    frappe.enqueue(run_item_price_sync, queue="long", timeout=600)


@frappe.whitelist()
def run_item_price_sync(
    item_code: Optional[str] = None,
    item_price_doc: Optional[ItemPrice] = None,
):
    sync = SynchroniseItemPrice(item_code=item_code, item_price_doc=item_price_doc)
    sync.run()
    return True


# ------------------------------------------------------------
# Main Sync Class
# ------------------------------------------------------------

class SynchroniseItemPrice(SynchroniseWooCommerce):
    """
    Synchronise ERPNext Item Prices with WooCommerce Products
    (Batch-based, timeout-safe)
    """

    def __init__(
        self,
        servers: List[WooCommerceServer | frappe._dict] = None,
        item_code: Optional[str] = None,
        item_price_doc: Optional[ItemPrice] = None,
    ):
        super().__init__(servers)
        self.item_code = item_code
        self.item_price_doc = item_price_doc
        self.wc_server = None
        self.item_price_list = []

    def run(self) -> None:
        for server in self.servers:
            self.wc_server = server
            self.get_erpnext_item_prices()
            self.sync_items_with_woocommerce_products()

    # --------------------------------------------------------

    def get_erpnext_item_prices(self) -> None:
        self.item_price_list = []

        if not (
            self.wc_server.enable_sync
            and self.wc_server.enable_price_list_sync
            and self.wc_server.price_list
        ):
            return

        ip = qb.DocType("Item Price")
        iwc = qb.DocType("Item WooCommerce Server")
        item = qb.DocType("Item")

        conditions = [
            ip.price_list == self.wc_server.price_list,
            iwc.woocommerce_server == self.wc_server.name,
            iwc.woocommerce_id.isnotnull(),
            iwc.enabled == 1,
            item.disabled == 0,
        ]

        if self.item_code:
            conditions.append(ip.item_code == self.item_code)

        self.item_price_list = (
            qb.from_(ip)
            .inner_join(iwc).on(iwc.parent == ip.item_code)
            .inner_join(item).on(item.name == ip.item_code)
            .select(
                ip.item_code,
                ip.price_list_rate,
                iwc.woocommerce_server,
                iwc.woocommerce_id,
            )
            .where(Criterion.all(conditions))
            .run(as_dict=True)
        )

    # --------------------------------------------------------
    # ⚠️ الاسم نفسه – implementation مختلف
    # --------------------------------------------------------

    def sync_items_with_woocommerce_products(self) -> None:
        total = len(self.item_price_list)

        for i in range(0, total, BATCH_SIZE):
            batch = self.item_price_list[i : i + BATCH_SIZE]

            frappe.enqueue(
                "woocommerce_softland.tasks.sync_item_prices._sync_item_price_batch",
                queue="long",
                timeout=600,
                batch=batch,
                wc_server=self.wc_server.name,
                item_price_doc=self.item_price_doc,
            )


# ------------------------------------------------------------
# Internal batch worker (NEW – safe to add)
# ------------------------------------------------------------

def _sync_item_price_batch(
    batch: list,
    wc_server: str,
    item_price_doc: Optional[ItemPrice] = None,
):
    wc_server = frappe.get_doc("WooCommerce Server", wc_server)

    for item_price in batch:
        try:
            wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
                domain=item_price["woocommerce_server"],
                resource_id=item_price["woocommerce_id"],
            )

            wc_product = frappe.get_doc(
                "WooCommerce Product",
                wc_product_name,
            )
            wc_product.load_from_db()

            price = (
                item_price_doc.price_list_rate
                if item_price_doc
                and item_price_doc.item_code == item_price["item_code"]
                and item_price_doc.price_list == wc_server.price_list
                else item_price["price_list_rate"]
            )

            current_price = float(wc_product.regular_price or 0)

            if current_price != price:
                wc_product.regular_price = price
                wc_product.save()

        except Exception:
            frappe.log_error(
                title="WooCommerce Price Sync Error",
                message=frappe.get_traceback(),
            )
