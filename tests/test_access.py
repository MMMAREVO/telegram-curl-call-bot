"""Доступ: регистрация по паролю, блокировка перебора, права админа, удаление пользователей."""
from tests import support
from tests.support import ADMIN_ID, PASSWORD, joined
from bot import admin_files, excel_store

USER = 100
STRANGER = 200

ADMIN_ONLY_CALLBACKS = [
    "menu:admin", "adm:camps", "adm:camp_add", "adm:camp:credits", "adm:camp_repl:credits",
    "adm:camp_del:credits", "adm:camp_delyes:credits", "adm:vl_up:uzum_auto", "adm:vl_down:uzum_auto",
    "adm:vl_del:uzum_auto", "adm:users", "adm:users_list", f"adm:user:{USER}", f"adm:uname:{USER}",
    f"adm:uphone:{USER}", f"adm:uadm:{USER}", f"adm:udel:{USER}", f"adm:udelyes:{USER}", "adm:users_up",
    "adm:users_apply", "adm:logs", "adm:logc:credits", "adm:logu:credits:0", "adm:logsearch", "menu:testplans",
    "tp_download", "tpdel_list", "tpdel:0", "tpdel_yes:0", "menu:upload_testcases", "menu:new_campaign",
    "newcamp:credits",
]


def sent_messages(out):
    return [r for r in out if r["method"] in ("SendMessage", "SendDocument")]


class RegistrationTest(support.BotTestCase):
    async def test_stranger_is_blocked(self):
        out = await self.press(STRANGER, "menu:call")
        self.assertEqual(sent_messages(out), [])
        self.assertTrue(any(r["method"] == "AnswerCallbackQuery" for r in out))
        out = await self.send(STRANGER, "/export")
        self.assertIn("Нет доступа", joined(out))

    async def test_full_registration(self):
        self.assertIn("пароль", joined(await self.send(STRANGER, "/start")))
        out = await self.send(STRANGER, PASSWORD)
        self.assertTrue(any(r["method"] == "DeleteMessage" for r in out), "пароль должен удаляться из чата")
        await self.send(STRANGER, "Новый")
        out = await self.send(STRANGER, "+998901234567")
        self.assertIn("без плюса", joined(out))
        out = await self.send(STRANGER, "998901234567")
        self.assertIn("Регистрация завершена", joined(out))
        self.assertNotIn("menu:admin", support.all_buttons(out))
        self.assertEqual((await excel_store.get_user(STRANGER))["phone"], "998901234567")

    async def test_password_bruteforce_lockout(self):
        await self.send(STRANGER, "/start")
        for _ in range(5):
            await self.send(STRANGER, "wrong")
        out = await self.send(STRANGER, PASSWORD)
        self.assertIn("Слишком много", joined(out))
        self.assertIsNone(await excel_store.get_user(STRANGER))

    async def test_registration_state_does_not_open_menus(self):
        # Незарегистрированный (например, удалённый админом) пользователь вводит /start и, пока бот
        # ждёт пароль, нажимает кнопки из старых сообщений — меню бота ему открываться не должны
        await excel_store.replace_test_plan_cases([
            {"test_plan": "Секретный план", "order": 1, "description": "Кейс", "campaign": "Кредиты"},
        ])
        await self.send(STRANGER, "/start")
        for data in ("menu:testcases", "menu:call", "menu:export"):
            with self.subTest(data=data):
                out = await self.press(STRANGER, data)
                self.assertEqual(sent_messages(out), [], f"{data}: {joined(out)!r}")


class AdminRightsTest(support.BotTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register(USER, "Обычный", "79991234567")

    async def test_regular_user_has_no_admin_access(self):
        self.assertNotIn("menu:admin", support.all_buttons(await self.send(USER, "/start")))
        for data in ADMIN_ONLY_CALLBACKS:
            with self.subTest(data=data):
                out = await self.press(USER, data)
                self.assertEqual(sent_messages(out), [], f"{data}: {joined(out)!r}")
        self.assertEqual(sent_messages(await self.send(USER, "/new_campaign")), [])
        # ничего не изменилось
        self.assertIsNotNone(support.campaigns.get("credits"))
        user = (await excel_store.list_users())[0]
        self.assertEqual((user["telegram_id"], user["is_admin"]), (USER, False))

    async def test_admin_from_bot_gets_and_loses_access(self):
        await self.press(ADMIN_ID, f"adm:uadm:{USER}")
        self.assertIn("menu:admin", support.all_buttons(await self.send(USER, "/start")))
        self.assertIn("Админка", joined(await self.press(USER, "menu:admin")))
        await self.press(ADMIN_ID, f"adm:uadm:{USER}")
        self.assertEqual(sent_messages(await self.press(USER, "menu:admin")), [])

    async def test_admin_cannot_demote_or_delete_self(self):
        await self.press(ADMIN_ID, f"adm:uadm:{USER}")
        self.assertIn("самого себя", joined(await self.press(USER, f"adm:uadm:{USER}")))
        self.assertIn("самого себя", joined(await self.press(USER, f"adm:udel:{USER}")))
        self.assertTrue((await excel_store.list_users())[0]["is_admin"])

    async def test_env_admin_cannot_be_demoted_from_bot(self):
        await self.register(ADMIN_ID, "Главный", "79991112233")
        out = await self.press(ADMIN_ID, f"adm:user:{ADMIN_ID}")
        self.assertNotIn(f"adm:uadm:{ADMIN_ID}", support.all_buttons(out))
        self.assertIn("(.env)", joined(out))

    async def test_deleted_user_loses_access_immediately(self):
        await self.press(ADMIN_ID, f"adm:udelyes:{USER}")
        self.assertIn("Нет доступа", joined(await self.send(USER, "/export")))
        self.assertEqual(sent_messages(await self.press(USER, "menu:call")), [])

    async def test_users_upload_cannot_remove_own_admin_rights(self):
        await self.press(ADMIN_ID, f"adm:uadm:{USER}")
        users = [u for u in await excel_store.list_users()]
        for u in users:
            u["is_admin"] = False
        await self.press(USER, "adm:users_up")
        out = await self.send(USER, document=("u.xlsx", admin_files.build_users_file(users)))
        self.assertIn("потеряете доступ", joined(out))
        self.assertTrue((await excel_store.list_users())[0]["is_admin"])


if __name__ == "__main__":
    import unittest
    unittest.main()
