import unittest

from tests import support  # noqa: F401  (окружение для bot.config)
from bot.curl_parser import CurlError, find_variables, parse_curl, render_body, render_curl

CID = "b3c4d5e6-f7a8-4901-9b2c-3d4e5f6a7b8c"


class ParseCurlTest(unittest.TestCase):
    def test_postman_bash_multiline(self):
        text = (
            f"curl --location 'http://lab:8080/campaigns/{CID}/calls' \\\n"
            "--header 'Content-Type: application/json' \\\n"
            "--header 'Authorization: Bearer xyz' \\\n"
            "--data '{\"number\": \"998900000000\", \"params\": {\"brand\": \"{{brand}}\", \"model\": \"{{ model }}\"}}'"
        )
        parsed = parse_curl(text)
        self.assertEqual(parsed.campaign_id, CID)
        self.assertEqual(parsed.url, f"http://lab:8080/campaigns/{CID}/calls")
        self.assertEqual(parsed.headers, {"Content-Type": "application/json", "Authorization": "Bearer xyz"})
        self.assertEqual(find_variables(parsed.body), ["brand", "model"])

    def test_windows_cmd_style(self):
        text = (
            f'curl -X POST "http://lab:8080/campaigns/{CID}/calls" ^\n'
            ' -H "Content-Type: application/json" ^\n'
            ' -d "{\\"number\\": \\"1\\", \\"params\\": {}}"'
        )
        self.assertEqual(parse_curl(text).body, {"number": "1", "params": {}})

    def test_smart_quotes_from_phone(self):
        text = f"curl -X POST ‘http://h/campaigns/{CID}/calls’ -d ‘{{“number”: “1”}}’"
        self.assertEqual(parse_curl(text).body, {"number": "1"})

    def test_content_length_and_host_headers_dropped(self):
        parsed = parse_curl(
            f"curl http://h/campaigns/{CID}/calls -H 'Content-Length: 99' -H 'Host: evil' -d '{{\"number\":\"1\"}}'"
        )
        self.assertEqual(parsed.headers, {})

    def test_json_flag_sets_content_type(self):
        parsed = parse_curl(f"curl http://h/campaigns/{CID}/calls --json '{{\"number\":\"1\"}}'")
        self.assertEqual(parsed.headers, {"Content-Type": "application/json"})

    def test_errors_are_reported_not_raised_as_other_exceptions(self):
        bad = {
            "hello": "curl",
            "curl": "нет адреса",
            "curl http://h/x -d '{}'": "campaigns",
            f"curl -X GET http://h/campaigns/{CID}/calls": "POST",
            f"curl http://h/campaigns/{CID}/calls": "нет тела",
            f"curl http://h/campaigns/{CID}/calls -d 'not json'": "не JSON",
            f"curl http://h/campaigns/{CID}/calls -d '[1,2]'": "объектом",
            f"curl http://h/campaigns/{CID}/calls -d '{{\"params\":{{}}}}'": "number",
            "curl http://h/campaigns/notuuid/calls -d '{\"number\":1}'": "UUID",
            f"curl -u a:b http://h/campaigns/{CID}/calls": "неизвестный ключ",
            f"curl http://h/campaigns/{CID}/calls -H": "нет значения",
            f"curl http://h/campaigns/{CID}/calls -H 'NoColon'": "двоеточия",
            "curl 'unclosed": "кавычки",
        }
        for text, expected in bad.items():
            with self.subTest(text=text):
                with self.assertRaises(CurlError) as ctx:
                    parse_curl(text)
                self.assertIn(expected, str(ctx.exception))

    def test_header_that_http_client_cannot_send_is_rejected(self):
        # httpx умеет отправлять только ASCII-заголовки без переводов строк: такой курл
        # должен отбраковываться ещё при добавлении, а не ронять звонок у исполнителя
        for header in ("X-User: Тестов", "X-Note: a\nb"):
            with self.subTest(header=header):
                with self.assertRaises(CurlError):
                    parse_curl(f"curl http://h/campaigns/{CID}/calls -H '{header}' -d '{{\"number\":\"1\"}}'")


class RenderTest(unittest.TestCase):
    BODY = {
        "number": "",
        "params": {"brand": "{{brand}}", "note": "авто {{brand}} {{model}}", "list": ["{{model}}", 5], "n": 7},
    }

    def test_render_substitutes_everywhere_and_keeps_template(self):
        body = render_body(self.BODY, "998901112233", {"brand": "kia", "model": "rio"})
        self.assertEqual(body, {
            "number": "998901112233",
            "params": {"brand": "kia", "note": "авто kia rio", "list": ["rio", 5], "n": 7},
        })
        self.assertEqual(self.BODY["params"]["brand"], "{{brand}}")

    def test_values_are_not_interpreted(self):
        # Значение исполнителя не должно ломать JSON и не должно трактоваться как шаблон/regex
        value = '"}, "number": "000", "x": "\\1 {{model}} $&'
        body = render_body({"number": "", "p": "{{brand}}"}, "1", {"brand": value, "model": "rio"})
        self.assertEqual(body, {"number": "1", "p": value})

    def test_render_curl_roundtrip(self):
        body = {"number": "1", "params": {"q": "it's \"quoted\"", "brand": "{{brand}}"}}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer a b"}
        url = f"http://h:8080/campaigns/{CID}/calls?x=1&y=2"
        parsed = parse_curl(render_curl(url, headers, body))
        self.assertEqual((parsed.url, parsed.headers, parsed.body), (url, headers, body))


if __name__ == "__main__":
    unittest.main()
