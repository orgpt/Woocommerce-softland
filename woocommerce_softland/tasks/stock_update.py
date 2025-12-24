import math
import json
import frappe
from woocommerce_softland.tasks.utils import APIWithRequestLogging

verify_ssl = not frappe._dev_server

def update_stock_levels_for_woocommerce_item(doc, method):
    """
    Trigger function: Runs on Save/Submit of Stock Entry, Sales Invoice, etc.
    Queues ONE job containing all unique items in the document.
    """
    if frappe.flags.in_test:
        return

    if doc.doctype in ("Stock Entry", "Stock Reconciliation", "Sales Invoice", "Delivery Note"):
        if doc.doctype == "Sales Invoice" and doc.update_stock == 0:
            return

        # 1. Get enabled WooCommerce servers
        enabled_servers = frappe.get_list(
            "WooCommerce Server", 
            filters={"enable_sync": 1, "enable_stock_level_synchronisation": 1}
        )
        if not enabled_servers:
            return

        # 2. Identify synced warehouses
        synced_warehouses = set()
        for server in enabled_servers:
            # Using get_doc (not cached) just to be safe with keys, or keep cached if reliable
            server_doc = frappe.get_cached_doc("WooCommerce Server", server.name)
            for wh_row in server_doc.warehouses:
                synced_warehouses.add(wh_row.warehouse)

        # 3. Identify warehouses touched by this document
        doc_warehouses = set()
        for row in doc.items:
            if row.get("warehouse"): doc_warehouses.add(row.warehouse)
            if row.get("s_warehouse"): doc_warehouses.add(row.s_warehouse)
            if row.get("t_warehouse"): doc_warehouses.add(row.t_warehouse)

        # 4. If no synced warehouses are involved, do nothing
        if not doc_warehouses.intersection(synced_warehouses):
            return

        # 5. Collect Unique Item Codes
        unique_item_codes = list(set([row.item_code for row in doc.items]))
        
        if unique_item_codes:
            # --- OPTIMIZATION: Enqueue ONE job with a LIST of items ---
            frappe.enqueue(
                "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
                enqueue_after_commit=True,
                item_code=unique_item_codes, 
                timeout=1800 # 30 minute timeout for large documents
            )


def update_stock_levels_for_all_enabled_items_in_background():
    """
    Get all enabled ERPNext Items and post stock updates to WooCommerce.
    Uses CHUNKING to prevent queue explosion.
    """
    erpnext_items = []
    current_page_length = 500
    start = 0

    # Get all items
    while current_page_length == 500:
        items = frappe.db.get_all(
            "Item",
            filters={"disabled": 0, "is_stock_item": 1},
            fields=["name"],
            start=start,
            page_length=500,
        )
        erpnext_items.extend(items)
        current_page_length = len(items)
        start += current_page_length

    # --- OPTIMIZATION: Process in Batches (Chunks) ---
    all_item_names = [item.name for item in erpnext_items]
    chunk_size = 50  # Process 50 items per Job
    
    for i in range(0, len(all_item_names), chunk_size):
        chunk = all_item_names[i : i + chunk_size]
        frappe.enqueue(
            "woocommerce_softland.tasks.stock_update.update_stock_levels_on_woocommerce_site",
            item_code=chunk,
            timeout=3600 # 1 hour timeout for batch processing
        )


@frappe.whitelist()
def update_stock_levels_on_woocommerce_site(item_code, force_sync=False):
    """
    Worker function. Can accept a single Item Code (string) OR a List of Item Codes.
    """
    # 1. Normalize input to a List
    items_to_process = []
    
    if isinstance(item_code, list):
        items_to_process = item_code
    elif isinstance(item_code, str):
        # Handle potential JSON string or simple string
        if item_code.startswith("["):
            try:
                items_to_process = json.loads(item_code)
            except:
                items_to_process = [item_code]
        else:
            items_to_process = [item_code]
    
    # 2. Iterate and Sync
    for single_item_code in items_to_process:
        try:
            sync_single_item(single_item_code, force_sync)
        except Exception as e:
            # Log error but CONTINUE to next item in the batch
            frappe.log_error(f"Stock Sync Failed for {single_item_code}", str(e))

    return True


def sync_single_item(item_code, force_sync):
    """
    The core logic for syncing a single item. 
    Moved here to keep the worker function clean.
    """
    item = frappe.get_doc("Item", item_code)

    if len(item.woocommerce_servers) == 0 or not item.is_stock_item or item.disabled:
        return
    
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

            # NOTE: Cache logic removed as per user request. 
            # It will always hit the API.

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

            parent_item_id = item.variant_of
            endpoint = ""
            
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

            if response.status_code != 200:
                error_message = f"Status Code not 200\nItem: {item_code}\nEndpoint: {endpoint}\nResponse: {response.text}"
                frappe.log_error("WooCommerce Sync Error", error_message)