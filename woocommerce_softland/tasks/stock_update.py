import math
import frappe
from woocommerce_softland.tasks.utils import APIWithRequestLogging

verify_ssl = not frappe._dev_server

def update_stock_levels_for_woocommerce_item(doc, method):
    if frappe.flags.in_test:
        return

    if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
        if doc.doctype == "Sales Invoice" and doc.update_stock == 0:
            return

        # --- OPTIMIZATION 1: Warehouse Filtering & Deduplication ---
        
        # 1. Get enabled WooCommerce servers
        enabled_servers = frappe.get_list(
            "WooCommerce Server", 
            filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1}
        )
        if not enabled_servers:
            return

        # 2. Identify synced warehouses from these servers
        synced_warehouses = set()
        for server in enabled_servers:
            server_doc = frappe.get_cached_doc("WooCommerce Server", server.name)
            for wh_row in server_doc.warehouses:
                synced_warehouses.add(wh_row.warehouse)

        # 3. Identify warehouses touched by this specific document
        doc_warehouses = set()
        for row in doc.items:
            if row.get("warehouse"): doc_warehouses.add(row.warehouse)
            if row.get("s_warehouse"): doc_warehouses.add(row.s_warehouse) # Source warehouse
            if row.get("t_warehouse"): doc_warehouses.add(row.t_warehouse) # Target warehouse

        # 4. If no synced warehouses are involved, do nothing
        if not doc_warehouses.intersection(synced_warehouses):
            return

        # 5. Queue jobs for unique items only (prevents duplicate jobs for same item)
        unique_item_codes = set([row.item_code for row in doc.items])
        for item_code in unique_item_codes:
            frappe.enqueue(
                "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
                enqueue_after_commit=True,
                item_code=item_code,
            )


def update_stock_levels_for_all_enabled_items_in_background():
    """
    Get all enabled ERPNext Items and post stock updates to WooCommerce
    """
    erpnext_items = []
    current_page_length = 500
    start = 0

    # Get all items, 500 records at a time
    while current_page_length == 500:
        items = frappe.db.get_all(
            "Item",
            filters={"disabled": 0},
            fields=["name"],
            start=start,
            page_length=500,
        )
        erpnext_items.extend(items)
        current_page_length = len(items)
        start += current_page_length

    for item in erpnext_items:
        frappe.enqueue(
            "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
            item_code=item.name,
        )


import math
import frappe
from woocommerce_softland.tasks.utils import APIWithRequestLogging

verify_ssl = not frappe._dev_server

def update_stock_levels_for_woocommerce_item(doc, method):
    """
    Trigger function: Runs on Save/Submit of Stock Entry, Sales Invoice, etc.
    """
    if frappe.flags.in_test:
        return

    if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
        if doc.doctype == "Sales Invoice" and doc.update_stock == 0:
            return

        # --- OPTIMIZATION: Warehouse Filtering & Deduplication ---
        
        # 1. Get enabled WooCommerce servers
        enabled_servers = frappe.get_list(
            "WooCommerce Server", 
            filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1}
        )
        if not enabled_servers:
            return

        # 2. Identify synced warehouses from these servers
        synced_warehouses = set()
        for server in enabled_servers:
            server_doc = frappe.get_cached_doc("WooCommerce Server", server.name)
            for wh_row in server_doc.warehouses:
                synced_warehouses.add(wh_row.warehouse)

        # 3. Identify warehouses touched by this specific document
        doc_warehouses = set()
        for row in doc.items:
            if row.get("warehouse"): doc_warehouses.add(row.warehouse)
            if row.get("s_warehouse"): doc_warehouses.add(row.s_warehouse) # Source
            if row.get("t_warehouse"): doc_warehouses.add(row.t_warehouse) # Target

        # 4. If no synced warehouses are involved, do nothing
        if not doc_warehouses.intersection(synced_warehouses):
            return

        # 5. Queue jobs for unique items only (prevents duplicate jobs for same item)
        unique_item_codes = set([row.item_code for row in doc.items])
        
        for item_code in unique_item_codes:
            frappe.enqueue(
                "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
                enqueue_after_commit=True,
                item_code=item_code,
            )


def update_stock_levels_for_all_enabled_items_in_background():
    """
    Get all enabled ERPNext Items and post stock updates to WooCommerce.
    This function is designed to be called by the Scheduler (Cron).
    """
    erpnext_items = []
    current_page_length = 500
    start = 0

    # Get all items, 500 records at a time
    while current_page_length == 500:
        items = frappe.db.get_all(
            "Item",
            filters={"disabled": 0, "is_stock_item": 1}, # Added is_stock_item filter for efficiency
            fields=["name"],
            start=start,
            page_length=500,
        )
        erpnext_items.extend(items)
        current_page_length = len(items)
        start += current_page_length

    for item in erpnext_items:
        frappe.enqueue(
            "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
            item_code=item.name,
        )


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code):
    """
    Updates stock levels of an item on all its associated WooCommerce sites.
    No Caching involved - direct API push.
    """
    item = frappe.get_doc("Item", item_code)

    if len(item.woocommerce_servers) == 0 or not item.is_stock_item or item.disabled:
        return False
    else:
        bins = frappe.get_list(
            "Bin", {"item_code": item_code}, ["name", "warehouse", "reserved_qty", "actual_qty"]
        )

        for wc_site in item.woocommerce_servers:
            if wc_site.woocommerce_id:
                woocommerce_id = wc_site.woocommerce_id
                woocommerce_server = wc_site.woocommerce_server
                wc_server = frappe.get_cached_doc("WooCommerce Server", woocommerce_server)

                if (
                    not wc_server
                    or not wc_server.enable_sync
                    or not wc_site.enabled
                    or not wc_server.enable_stock_level_synchronisation
                ):
                    continue

                # Calculate stock
                current_stock_qty = math.floor(
                    sum(
                        bin.actual_qty
                        if not wc_server.subtract_reserved_stock
                        else bin.actual_qty - bin.reserved_qty
                        for bin in bins
                        if bin.warehouse in [row.warehouse for row in wc_server.warehouses]
                    )
                )

                wc_api = APIWithRequestLogging(
                    url=wc_server.woocommerce_server_url,
                    consumer_key=wc_server.api_consumer_key,
                    consumer_secret=wc_server.api_consumer_secret,
                    version="wc/v3",
                    timeout=40,
                    verify_ssl=verify_ssl,
                )

                data_to_post = {
                    "stock_quantity": current_stock_qty
                }

                try:
                    parent_item_id = item.variant_of
                    if parent_item_id:
                        parent_item = frappe.get_doc("Item", parent_item_id)
                        parent_woocommerce_id = None
                        for parent_wc_site in parent_item.woocommerce_servers:
                            if parent_wc_site.woocommerce_server == woocommerce_server:
                                parent_woocommerce_id = parent_wc_site.woocommerce_id
                                break
                        if not parent_woocommerce_id:
                            continue
                        endpoint = f"products/{parent_woocommerce_id}/variations/{woocommerce_id}"
                    else:
                        endpoint = f"products/{woocommerce_id}"
                    
                    response = wc_api.put(endpoint=endpoint, data=data_to_post)

                except Exception as err:
                    error_message = f"{frappe.get_traceback()}\n\nData in PUT request: \n{str(data_to_post)}"
                    frappe.log_error("WooCommerce Error", error_message)
                    raise err

                if response.status_code != 200:
                    error_message = f"Status Code not 200\n\nData in PUT request: \n{str(data_to_post)}"
                    error_message += (
                        f"\n\nResponse: \n{response.status_code}\nResponse Text: {response.text}\nRequest URL: {response.request.url}\nRequest Body: {response.request.body}"
                        if response is not None
                        else ""
                    )
                    frappe.log_error("WooCommerce Error", error_message)
                    raise ValueError(error_message)

        return True