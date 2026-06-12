# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class CommerceBridgeSyncWizard(models.TransientModel):
    _name = "commerce.bridge.sync.wizard"
    _description = "CommerceBridge Guided Sync Wizard"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True)
    operation = fields.Selection([
        ("readiness", "Check readiness"),
        ("stores", "Import stores"),
        ("products", "Import products"),
        ("orders", "Import orders"),
        ("stock", "Export stock"),
        ("inventory_drift", "Check inventory drift"),
        ("process_queue", "Process queue now"),
    ], required=True, default="readiness")
    import_from = fields.Datetime(string="Import From")
    note = fields.Text(readonly=True, default=lambda self: _(
        "Recommended first run: 1) Test connection, 2) Import stores, 3) Check readiness, "
        "4) Import products, 5) Import orders, 6) Export stock."
    ))

    def action_run(self):
        self.ensure_one()
        inst = self.instance_id
        if self.operation == "readiness":
            return inst.action_run_readiness()
        if self.operation == "stores":
            inst.action_import_stores()
            return {"type": "ir.actions.act_window_close"}
        if self.operation == "products":
            if self.import_from:
                inst.import_product_from = self.import_from
            return inst.action_enqueue_product_import()
        if self.operation == "orders":
            if self.import_from:
                inst.import_order_from = self.import_from
            return inst.action_enqueue_order_import()
        if self.operation == "stock":
            return inst.action_enqueue_stock_export()
        if self.operation == "inventory_drift":
            return inst.action_reconcile_inventory()
        if self.operation == "process_queue":
            return inst.action_process_queue()
        raise UserError(_("Unsupported wizard operation."))
