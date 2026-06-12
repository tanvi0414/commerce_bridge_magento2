# -*- coding: utf-8 -*-
{
    "name": "Magento 2 Connector for Odoo",
    "summary": "Operations-grade Magento 2 / Adobe Commerce connector for Odoo",
    "description": """
CommerceBridge Magento 2 is an operations-grade connector for Magento 2 / Adobe Commerce.
It focuses on safe go-live, recoverable queue jobs, non-technical mapping screens,
readiness checks, product/order/customer sync foundations, inventory export, shipments,
logs, reconciliation, and advanced B2B/MSI scaffolding.
    """,
    "version": "18.0.1.0.0",
    "category": "Sales/Commerce",
    "author": "CoDE2",
    "license": "OPL-1",
    "price": 389.00,
    "currency": "USD",
    "depends": [
        "base",
        "mail",
        "product",
        "sale_management",
        "stock",
        "account",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/default_products.xml",
        "data/ir_cron.xml",
        "views/menu_views.xml",
        "views/instance_views.xml",
        "views/mapping_views.xml",
        "views/binding_views.xml",
        "views/queue_views.xml",
        "views/readiness_views.xml",
        "views/drift_views.xml",
        "views/stock_picking_views.xml",
        "views/wizard_views.xml",
    ],
    "images": ["static/description/banner.gif"],
    "installable": True,
    "application": True,
    
}
