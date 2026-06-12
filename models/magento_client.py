# -*- coding: utf-8 -*-
import json
import logging
import time
from urllib.parse import quote

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class MagentoApiError(UserError):
    """Raised when Magento returns a non-successful response."""


class MagentoClient:
    """Small Magento 2 / Adobe Commerce REST client.

    The connector intentionally uses plain REST calls instead of a heavy SDK so the
    module remains easy to install on Odoo.sh/on-premise environments.
    """

    def __init__(self, base_url, token, timeout=60, verify_ssl=True, store_code=None):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = int(timeout or 60)
        self.verify_ssl = bool(verify_ssl)
        self.store_code = (store_code or "all").strip() or "all"
        if not self.base_url:
            raise MagentoApiError("Magento base URL is empty.")
        if not self.token:
            raise MagentoApiError("Magento access token is empty.")

    def _url(self, path):
        path = path if path.startswith("/") else "/" + path
        if path.startswith("/rest/"):
            return self.base_url + path
        return "%s/rest/%s%s" % (self.base_url, quote(self.store_code), path)

    def _headers(self):
        return {
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CommerceBridge-Magento2-Odoo/19.0",
        }

    def request(self, method, path, params=None, payload=None, timeout=None):
        url = self._url(path)
        start = time.time()
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=self._headers(),
                params=params or {},
                data=json.dumps(payload) if payload is not None else None,
                timeout=timeout or self.timeout,
                verify=self.verify_ssl,
            )
        except requests.exceptions.RequestException as exc:
            raise MagentoApiError("Magento connection failed: %s" % exc) from exc

        duration_ms = int((time.time() - start) * 1000)
        content_type = response.headers.get("Content-Type", "")
        body = response.text or ""
        parsed = None
        if "json" in content_type.lower() or body.startswith("{") or body.startswith("["):
            try:
                parsed = response.json()
            except Exception:
                parsed = None

        if response.status_code >= 400:
            message = self._extract_error_message(parsed, body)
            raise MagentoApiError(
                "Magento API error %s on %s %s (%sms): %s"
                % (response.status_code, method.upper(), path, duration_ms, message)
            )
        if parsed is not None:
            return parsed
        return body

    @staticmethod
    def _extract_error_message(parsed, body):
        if isinstance(parsed, dict):
            msg = parsed.get("message") or parsed.get("error_description") or parsed.get("error")
            params = parsed.get("parameters")
            if params and msg:
                try:
                    return "%s | Parameters: %s" % (msg, json.dumps(params, ensure_ascii=False))
                except Exception:
                    return "%s | Parameters: %s" % (msg, params)
            if msg:
                return str(msg)
        return (body or "Unknown Magento error")[:2000]

    @staticmethod
    def search_params(page=1, page_size=100, filters=None, sort_field=None, sort_direction="ASC"):
        params = {
            "searchCriteria[currentPage]": page,
            "searchCriteria[pageSize]": page_size,
        }
        for group_index, group in enumerate(filters or []):
            # group can be a list of filters OR a single tuple.
            if isinstance(group, tuple):
                group = [group]
            for filter_index, item in enumerate(group):
                field, value, condition = item
                base = "searchCriteria[filter_groups][%s][filters][%s]" % (group_index, filter_index)
                params[base + "[field]"] = field
                params[base + "[value]"] = value
                params[base + "[condition_type]"] = condition or "eq"
        if sort_field:
            params["searchCriteria[sortOrders][0][field]"] = sort_field
            params["searchCriteria[sortOrders][0][direction]"] = sort_direction or "ASC"
        return params

    def paginated(self, path, item_key="items", page_size=100, filters=None, sort_field=None, sort_direction="ASC", max_pages=0):
        page = 1
        while True:
            params = self.search_params(page=page, page_size=page_size, filters=filters, sort_field=sort_field, sort_direction=sort_direction)
            result = self.request("GET", path, params=params)
            if isinstance(result, list):
                items = result
                total = len(items)
            else:
                items = result.get(item_key, []) if isinstance(result, dict) else []
                total = int(result.get("total_count", len(items)) or 0) if isinstance(result, dict) else len(items)
            for item in items:
                yield item
            if not items or len(items) < page_size or (page * page_size) >= total:
                break
            page += 1
            if max_pages and page > max_pages:
                break

    # Store/config endpoints
    def get_websites(self):
        return self.request("GET", "/V1/store/websites")

    def get_store_groups(self):
        return self.request("GET", "/V1/store/storeGroups")

    def get_store_views(self):
        return self.request("GET", "/V1/store/storeViews")

    def get_modules(self):
        return self.request("GET", "/V1/modules")

    def get_payment_methods(self):
        return self.request("GET", "/V1/paymentmethod/list")

    def get_shipping_methods(self):
        # Magento does not always expose a clean admin-wide shipping-method list.
        # This endpoint works on many stores, but the connector treats failure as warning.
        return self.request("GET", "/V1/carts/mine/shipping-methods")

    # Catalog endpoints
    def iter_products(self, updated_from=None, page_size=100):
        filters = []
        if updated_from:
            filters.append(("updated_at", updated_from, "gteq"))
        return self.paginated("/V1/products", page_size=page_size, filters=filters, sort_field="updated_at", sort_direction="ASC")

    def get_product(self, sku):
        return self.request("GET", "/V1/products/%s" % quote(str(sku), safe=""))

    def iter_categories(self, page_size=100):
        return self.paginated("/V1/categories/list", page_size=page_size, sort_field="entity_id", sort_direction="ASC")

    # Sales endpoints
    def iter_orders(self, updated_from=None, created_from=None, page_size=100, statuses=None):
        filters = []
        if updated_from:
            filters.append(("updated_at", updated_from, "gteq"))
        if created_from:
            filters.append(("created_at", created_from, "gteq"))
        if statuses:
            filters.append(("status", ",".join(statuses), "in"))
        return self.paginated("/V1/orders", page_size=page_size, filters=filters, sort_field="updated_at", sort_direction="ASC")

    def get_order(self, order_id):
        return self.request("GET", "/V1/orders/%s" % quote(str(order_id), safe=""))

    def create_shipment(self, order_id, items=None, tracks=None, notify=True, comment=None):
        payload = {
            "items": items or [],
            "tracks": tracks or [],
            "notify": bool(notify),
            "appendComment": bool(comment),
        }
        if comment:
            payload["comment"] = {"comment": comment, "is_visible_on_front": 0}
        return self.request("POST", "/V1/order/%s/ship" % quote(str(order_id), safe=""), payload=payload)

    # Inventory endpoints
    def get_sources(self):
        return self.paginated("/V1/inventory/sources", page_size=100)

    def get_source_items(self, skus=None, source_code=None, page_size=100):
        filters = []
        if skus:
            filters.append(("sku", ",".join([str(s) for s in skus]), "in"))
        if source_code:
            filters.append(("source_code", source_code, "eq"))
        return list(self.paginated("/V1/inventory/source-items", page_size=page_size, filters=filters))

    def save_source_items(self, source_items):
        return self.request("POST", "/V1/inventory/source-items", payload={"sourceItems": source_items})

    def update_classic_stock_item(self, sku, qty, is_in_stock=True):
        payload = {"stockItem": {"qty": float(qty), "is_in_stock": bool(is_in_stock)}}
        return self.request("PUT", "/V1/products/%s/stockItems/1" % quote(str(sku), safe=""), payload=payload)

    # Price endpoints
    def update_base_prices(self, prices):
        # prices: [{"price": 10, "store_id": 0, "sku": "ABC"}]
        return self.request("POST", "/V1/products/base-prices", payload={"prices": prices})

    def update_special_prices(self, prices):
        return self.request("POST", "/V1/products/special-price", payload={"prices": prices})
