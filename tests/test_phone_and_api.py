import unittest
from unittest import mock

import httpx

from tests import support  # noqa: F401
from bot import api_client
from bot.phone import validate_phone


class PhoneTest(unittest.TestCase):
    def test_valid(self):
        for phone in ("998901234567", "79991234567", "992901234567", "1234567"):
            with self.subTest(phone=phone):
                self.assertEqual(validate_phone(f"  {phone} "), (phone, None))

    def test_invalid(self):
        for phone in ("", "+79991234567", "7999 123 45 67", "7(999)1234567", "99890123456", "7999123456",
                      "123456", "1234567890123456", "７９９９１２３４５６７"):
            with self.subTest(phone=phone):
                number, error = validate_phone(phone)
                self.assertIsNone(number)
                self.assertTrue(error)


class TriggerCallTest(unittest.IsolatedAsyncioTestCase):
    """trigger_call не должен падать исключением — только возвращать CallResult(ok=False)."""

    async def _call(self, handler, headers=None):
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with mock.patch.object(api_client.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw)):
            try:
                return await api_client.trigger_call("http://h/campaigns/x/calls", headers or {}, {"number": "1"})
            except Exception as exc:
                self.fail(f"trigger_call упал вместо CallResult(ok=False): {type(exc).__name__}: {exc}")

    async def test_success(self):
        seen = {}

        def handler(request):
            seen["body"] = request.content
            seen["ct"] = request.headers.get("content-type")
            return httpx.Response(200, json={"status": "ok", "data": {"sid": "S1"}})

        result = await self._call(handler)
        self.assertTrue(result.ok)
        self.assertEqual(result.sid, "S1")
        self.assertEqual(seen["body"], b'{"number":"1"}')
        self.assertEqual(seen["ct"], "application/json")

    async def test_http_error_and_bad_status(self):
        self.assertFalse((await self._call(lambda r: httpx.Response(500, text="boom"))).ok)
        self.assertFalse((await self._call(lambda r: httpx.Response(200, json={"status": "fail"}))).ok)
        self.assertFalse((await self._call(lambda r: httpx.Response(200, text="<html>"))).ok)

    async def test_network_error(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        self.assertFalse((await self._call(handler)).ok)

    async def test_unexpected_json_shape(self):
        for payload in ([1, 2], "ok", None, {"status": "ok", "data": "x"}):
            with self.subTest(payload=payload):
                result = await self._call(lambda r, p=payload: httpx.Response(200, json=p))
                self.assertIsInstance(result, api_client.CallResult)

    async def test_non_ascii_header(self):
        result = await self._call(lambda r: httpx.Response(200, json={"status": "ok"}), {"X-User": "Тестов"})
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
