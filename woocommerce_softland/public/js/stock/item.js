frappe.ui.form.on('Item', {
	refresh: function(frm) {

		frm.add_custom_button(
			__("Sync this Item's Stock Levels to WooCommerce"),
			function () {
				frm.trigger("sync_item_stock");
			},
			__('Actions')
		);

		frm.add_custom_button(
			__("Sync this Item's Price to WooCommerce NOw"),
			function () {
				frm.trigger("sync_item_price");
			},
			__('Actions')
		);

		frm.add_custom_button(
			__("Sync this Item with WooCommerce NOW"),
			function () {
				frm.trigger("sync_item");
			},
			__('Actions')
		);
	},

	/* ---------------- STOCK ---------------- */
	sync_item_stock: function(frm) {
		frappe.dom.freeze(__("Queuing stock sync..."));
		frappe.call({
			method: "woocommerce_softland.tasks.stock_update.enqueue_manual_item_stock_sync",
			args: {
				item_code: frm.doc.name
			},
			callback: function() {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Stock sync queued safely'),
					indicator: 'green'
				}, 5);
			},
			error: () => {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Error occurred. See Error Log.'),
					indicator: 'red'
				}, 5);
			}
		});
	},

	/* ---------------- PRICE ---------------- */
	sync_item_price: function(frm) {
		frappe.dom.freeze(__("Queuing price sync..."));
		frappe.call({
			method: "woocommerce_softland.tasks.sync_item_prices.enqueue_manual_item_price_sync",
			args: {
				item_code: frm.doc.name
			},
			callback: function() {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Price sync queued safely'),
					indicator: 'green'
				}, 5);
			},
			error: () => {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Error occurred. See Error Log.'),
					indicator: 'red'
				}, 5);
			}
		});
	},

	/* ---------------- FULL ITEM ---------------- */
	sync_item: function(frm) {
		frappe.dom.freeze(__("Queuing item sync..."));
		frappe.call({
			method: "woocommerce_softland.tasks.sync_items.enqueue_manual_item_sync",
			args: {
				item_code: frm.doc.name
			},
			callback: function() {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Item sync queued safely'),
					indicator: 'green'
				}, 5);
			},
			error: () => {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('Error occurred. See Error Log.'),
					indicator: 'red'
				}, 5);
			}
		});
	},
});

frappe.ui.form.on('Item WooCommerce Server', {
	view_product: function(frm, cdt, cdn) {
		let row = locals[cdt][cdn];
		frappe.set_route(
			"Form",
			"WooCommerce Product",
			`${row.woocommerce_server}~${row.woocommerce_id}`
		);
	}
});