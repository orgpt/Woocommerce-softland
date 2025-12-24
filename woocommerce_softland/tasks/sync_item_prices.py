from time import sleep
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


# ----------------------------------------------------------------------
# HOOK (SAFE)
# ----------------------------------------------------------------------
def update_item_price_for_woocommerce_item_from_hook(doc, method):
    """
    Triggered from Item Price hooks.
    We only QUEUE a background job safely.
    """
    if frappe.flags.in_test:
        return

    if doc.doctype != "Item Price":
        return

    enqueue_manual_item_price_sync(doc.item_code)


# ----------------------------------------------------------------------
# BACKGROUND WRAPPER (SCHEDULER / MANUAL)
# ----------------------------------------------------------------------
@frappe.whitelist()
def run_item_price_sync_in_background():
    if frappe.cache().get_value("woo_price_sync_lock"):
        return

    frappe.cache().set_value(
        "woo_price_sync_lock",
        1,
        expires_in_sec=14400  # 4 hours
    )

    try:
        run_item_price_sync()
    finally:
        frappe.cache().delete_value("woo_price_sync_lock")


# ----------------------------------------------------------------------
# CORE SYNC ENTRY
# ----------------------------------------------------------------------
@frappe.whitelist()
def run_item_price_sync(
    item_code: Optional[str] = None,
    item_price_doc: Optional[ItemPrice] = None
):
    sync = SynchroniseItemPrice(
        item_code=item_code,
        item_price_doc=item_price_doc
    )
    sync.run()
    return True


# ----------------------------------------------------------------------
# SYNC CLASS
# ----------------------------------------------------------------------
class SynchroniseItemPrice(SynchroniseWooCommerce):
    """
    Class for managing synchronisation of ERPNext Item Prices
    with WooCommerce Products
    """

    item_code: Optional[str]
    item_price_list: List

    def __init__(
        self,
        servers: List[WooCommerceServer | frappe._dict] = None,
        item_code: Optional[str] = None,
        item_price_doc: Optional[ItemPrice] = None,
    ) -> None:
        super().__init__(servers)
        self.item_code = item_code
        self.item_price_doc = item_price_doc
        self.wc_server = None
        self.item_price_list = []

    def run(self) -> None:
        """
        Run synchronisation
        """
        for server in self.servers:
            self.wc_server = server
            self.get_erpnext_item_prices()
            self.sync_items_with_woocommerce_products()

    def get_erpnext_item_prices(self) -> None:
        """
        Get list of ERPNext Item Prices to synchronise
        """
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
            item.disabled == 0,
            iwc.woocommerce_id.isnotnull(),
            iwc.enabled == 1,
        ]

        if self.item_code:
            conditions.append(ip.item_code == self.item_code)

        self.item_price_list = (
            qb.from_(ip)
            .inner_join(iwc).on(iwc.parent == ip.item_code)
            .inner_join(item).on(item.name == ip.item_code)
            .select(
                ip.name,
                ip.item_code,
                ip.price_list_rate,
                iwc.woocommerce_server,
                iwc.woocommerce_id,
            )
            .where(Criterion.all(conditions))
            .run(as_dict=True)
        )

    def sync_items_with_woocommerce_products(self) -> None:
        """
        Synchronise Item Prices with WooCommerce Products
        """
        for item_price in self.item_price_list:
            wc_product_name = generate_woocommerce_record_name_from_domain_and_id(
                domain=item_price.woocommerce_server,
                resource_id=item_price.woocommerce_id,
            )

            wc_product = frappe.get_doc(
                {"doctype": "WooCommerce Product", "name": wc_product_name}
            )

            try:
                wc_product.load_from_db()

                price_list_rate = (
                    self.item_price_doc.price_list_rate
                    if self.item_price_doc
                    and self.item_price_doc.price_list == self.wc_server.price_list
                    else item_price.price_list_rate
                )

                if not wc_product.regular_price:
                    wc_product.regular_price = 0

                wc_regular_price = (
                    float(wc_product.regular_price)
                    if isinstance(wc_product.regular_price, str)
                    else wc_product.regular_price
                )

                if wc_regular_price != price_list_rate:
                    wc_product.regular_price = price_list_rate
                    wc_product.save()

            except Exception:
                error_message = (
                    f"{frappe.get_traceback()}\n\n"
                    f"Product Data:\n{wc_product.as_dict()}"
                )
                frappe.log_error(
                    "WooCommerce Error: Price List Sync",
                    error_message
                )

            sleep(self.wc_server.price_list_delay_per_item)


# ----------------------------------------------------------------------
# MANUAL SINGLE-ITEM TRIGGER (SAFE)
# ----------------------------------------------------------------------
@frappe.whitelist()
def enqueue_manual_item_price_sync(item_code):
    cache_key = f"woo_price_manual_{item_code}"

    if frappe.cache().get_value(cache_key):
        return

    frappe.cache().set_value(
        cache_key,
        1,
        expires_in_sec=1800  # 30 minutes
    )

    frappe.enqueue(
        "woocommerce_softland.tasks.sync_item_prices.run_item_price_sync",
        queue="long",
        timeout=900,
        item_code=item_code,
    )
