from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from bot import roleplay as rp
from bot.config import Config, conversation_defaults, defaults
from bot.server import create_app


class PromptCompiler(unittest.TestCase):
    def setUp(self):
        self.cfg = defaults()
        self.pack = self.cfg["roleplay"]
        self.card = self.pack["characters"][0]
        self.preset = self.pack["presets"][0]

    def compile(self, text="你好", **kwargs):
        return rp.compile_prompt(self.cfg, "", [{"role": "user", "content": text}], **kwargs)

    def text(self, prompt):
        return "\n".join(m["content"] for m in prompt["messages"])

    def book(self, entries):
        self.pack["worldbooks"] = [{"id": "test_book", "name": "测试世界", "entries": entries}]
        self.card["worldbook_ids"] = ["test_book"]

    def test_defaults_validate_and_runtime_keeps_last_user_role(self):
        rp.validate(self.pack)
        result = self.compile()
        self.assertEqual(result["messages"][-1]["role"], "user")
        self.assertIn("运行协议", result["messages"][-2]["content"])

    def test_explicit_full_character_core_survives_profile_rag_without_changing_other_profiles(self):
        self.cfg['_memory_options']={'rag_profiles':True}
        self.card['personality']='完整原卡核心\n'+'不同的人设细节。'*180+'必须保留的结尾'
        self.card['extensions']={'qq_companion':{'full_profile_fields':['personality']}}
        preserved=self.card['personality']
        result=self.compile()
        self.assertIn(preserved,self.text(result))
        self.assertEqual(result['profile_retrieval']['character.personality']['selected_chars'],len(preserved))
        self.card['extensions']={}
        result=self.compile()
        self.assertNotIn(preserved,self.text(result))
        self.assertLess(result['profile_retrieval']['character.personality']['selected_chars'],len(preserved))

    def test_bound_character_persona_and_preset_override_global(self):
        self.cfg["life"]["enabled"] = False  # Legacy multi-character mode is retained when account life is off.
        conv = conversation_defaults()
        conv.update(character_id="traveler", preset_id="story", user_persona_id="self")
        self.cfg["conversations"]["private:123456789"] = conv
        result = rp.compile_prompt(self.cfg, "private:123456789", [{"role":"user","text":"你是谁"}])
        self.assertEqual(result["resolved"]["character"], "灯下的旅人")
        self.assertEqual(result["generation"]["max_tokens"], 4500)
        self.assertNotIn(self.card["description"], self.text(result))

    def test_macro_expansion_is_literal_and_does_not_evaluate_script(self):
        self.preset["main_prompt"] = "{{char}} 与 {{user}} / {{date}} / {{execute('rm')}}"
        result = self.compile()
        self.assertIn("栖 与 我", self.text(result))
        self.assertIn("{{execute('rm')}}", self.text(result))
        self.assertTrue(result["warnings"])

    def test_character_prompt_override_is_opt_in_and_original_is_expanded(self):
        self.card["system_prompt"] = "CARD-OVERRIDE {{original}}"
        self.assertNotIn("CARD-OVERRIDE", self.text(self.compile()))
        self.preset["use_character_prompt"] = True
        text = self.text(self.compile())
        self.assertIn("CARD-OVERRIDE", text)
        self.assertIn("身份与", text)

    def test_creator_notes_and_extensions_never_become_instructions(self):
        self.card["creator_notes"] = "CREATOR-NOTE-SECRET"
        self.card["extensions"] = {"script": "EXECUTE-THIS"}
        text = self.text(self.compile())
        self.assertNotIn("CREATOR-NOTE-SECRET", text)
        self.assertNotIn("EXECUTE-THIS", text)

    def test_keyword_secondary_and_constant_activation(self):
        a = rp.entry("双条件"); a.update(keys=["书店"], secondary_keys=["铜铃"], selective=True, content="DOUBLE")
        b = rp.entry("常驻"); b.update(constant=True, selective=True, content="ALWAYS")
        self.book([a,b])
        self.assertNotIn("DOUBLE", self.text(self.compile("书店")))
        self.assertIn("DOUBLE", self.text(self.compile("书店 铜铃")))
        self.assertIn("ALWAYS", self.text(self.compile("无关键词")))

    def test_recursive_activation_and_exclusion(self):
        a=rp.entry("一");a.update(keys=["门"],content="铜铃")
        b=rp.entry("二");b.update(keys=["铜铃"],content="RECURSIVE-FACT")
        self.book([a,b])
        result=self.compile("门")
        self.assertIn("RECURSIVE-FACT",self.text(result))
        self.assertEqual(next(i["wave"] for i in result["lore"]["included"] if i["name"]=="二"),1)
        a["exclude_recursion"]=True
        self.assertNotIn("RECURSIVE-FACT",self.text(self.compile("门")))

    def test_priority_and_budget_do_not_trigger_from_rejected_content(self):
        a=rp.entry("太长");a.update(keys=["门"],content="TRIGGER"+"x"*200,priority=200)
        b=rp.entry("短");b.update(keys=["门"],content="SHORT",priority=100)
        c=rp.entry("衍生");c.update(keys=["TRIGGER"],content="PHANTOM",priority=300)
        self.book([a,b,c]);self.preset["lore_budget"]=20
        result=self.compile("门")
        self.assertIn("SHORT",self.text(result));self.assertNotIn("PHANTOM",self.text(result))
        self.assertEqual(result["lore"]["skipped"][0]["name"],"太长")

    def test_disabled_module_is_reported_as_not_injected(self):
        a=rp.entry("背景");a.update(constant=True,content="DISABLED-LORE")
        self.book([a])
        next(s for s in self.preset["sections"] if s["id"]=="lore_before")["enabled"]=False
        result=self.compile()
        self.assertNotIn("DISABLED-LORE",self.text(result));self.assertFalse(result["lore"]["included"])
        self.assertIn("关闭",result["lore"]["skipped"][0]["reason"])

    def test_author_note_and_lore_depth_count_real_messages(self):
        self.card["first_mes"]="";self.card["mes_example"]=""
        self.preset.update(author_note="NOTE",author_depth=1)
        a=rp.entry("深度");a.update(constant=True,content="DEPTH",position="in_chat",depth=1)
        self.book([a])
        result=rp.compile_prompt(self.cfg,"",[{"role":"user","content":"u1"},{"role":"assistant","content":"a1"},{"role":"user","content":"u2"}])
        messages=result["messages"]
        # Runtime/post-history controls may sit between these; depth counts real turns.
        relevant=[m["content"] for m in messages if m["content"] in {"u1","a1","NOTE","DEPTH","u2"}]
        self.assertEqual(relevant,["u1","a1","NOTE","DEPTH","u2"])
        self.assertEqual(messages[-1]["content"],"u2")

    def test_context_budget_removes_oldest_history_and_keeps_latest(self):
        self.preset["context_chars"]=4000
        history=[{"role":"user","content":"OLD"+"x"*2500},{"role":"assistant","content":"y"*2500},{"role":"user","content":"LATEST"}]
        result=rp.compile_prompt(self.cfg,"",history)
        self.assertLessEqual(result["characters"],4000)
        self.assertNotIn("OLD",self.text(result));self.assertIn("LATEST",self.text(result))
        self.assertGreater(result["dropped_messages"],0)

    def test_examples_are_separate_references_and_history_macros_are_not_expanded(self):
        self.card["mes_example"]="<START>\n{{user}}: EXAMPLE-Q\n{{char}}: EXAMPLE-A"
        result=self.compile("{{char}} is literal user input")
        self.assertFalse(any(m["role"]=="assistant" and "EXAMPLE-A" in m["content"] for m in result["messages"]))
        self.assertTrue(any(m["role"]=="system" and "EXAMPLE-A" in m["content"] and "独立虚构示例" in m["content"] for m in result["messages"]))
        self.assertIn("{{char}} is literal user input",self.text(result))

    def test_passive_trigger_is_actual_user_once_and_proactive_remains_scheduler(self):
        result=self.compile('LAST-REAL-USER')
        self.assertEqual(result['messages'][-1],{'role':'user','content':'LAST-REAL-USER'})
        self.assertEqual(sum(m['content']=='LAST-REAL-USER' for m in result['messages']),1)
        self.assertNotIn('[本轮触发]',self.text(result))
        proactive=self.compile('LAST-REAL-USER',proactive=True)
        self.assertIn('[调度事件]',proactive['messages'][-1]['content'])
        self.assertTrue(any(m['content']=='LAST-REAL-USER' for m in proactive['messages']))

    def test_unsent_greeting_is_not_a_real_assistant_turn(self):
        self.card['first_mes']='UNSENT-GREETING'
        result=self.compile('在吗')
        self.assertFalse(any(m['role']=='assistant' and 'UNSENT-GREETING' in m['content'] for m in result['messages']))
        self.assertIn('这句话没有实际发送',self.text(result))

    def test_runtime_provides_actual_configured_model_and_memory_sources(self):
        self.cfg['model']='deepseek-test-identity'
        text=self.text(self.compile('你是什么模型'))
        self.assertIn('deepseek-test-identity',text)
        self.assertIn('助手自己的推测',text)

    def test_library_and_bindings_validation_reject_missing_refs(self):
        with self.assertRaises(ValueError):
            bad=copy.deepcopy(self.pack);bad["active_character"]="missing";rp.validate(bad)
        conv=conversation_defaults();conv["character_id"]="missing"
        with self.assertRaises(ValueError):rp.bindings(self.pack,{"private:123456789":conv})


class CardInterchange(unittest.TestCase):
    def setUp(self):self.pack=rp.defaults();self.card=self.pack["characters"][1]

    def test_json_v2_roundtrip_preserves_embedded_lore_and_metadata(self):
        self.card["extensions"]={"unknown_safe_metadata":{"x":1}}
        payload=rp.export_card(self.card,self.pack)
        imported=rp.import_card(payload)
        self.assertEqual(payload["spec"],"chara_card_v2")
        self.assertEqual(imported["character"]["description"],self.card["description"])
        self.assertEqual(imported["character"]["extensions"],self.card["extensions"])
        self.assertEqual(imported["worldbooks"][0]["entries"][0]["keys"],["书店","铜铃"])

    def test_png_roundtrip_preserves_chinese_and_valid_portrait(self):
        raw=rp.export_png(self.card,self.pack)
        payload,portrait=rp.extract_card(raw)
        imported=rp.import_card(payload,portrait)
        self.assertEqual(imported["character"]["name"],"灯下的旅人")
        candidate=rp.defaults();candidate["characters"].append(imported["character"]);candidate["worldbooks"]+=imported["worldbooks"]
        rp.validate(candidate)
        self.assertTrue(portrait.startswith("data:image/png;base64,"))
        png_again=rp.export_png(imported["character"],candidate)
        self.assertEqual(rp.extract_card(png_again)[0]["data"]["name"],"灯下的旅人")

    def test_corrupt_png_and_compressed_text_bomb_are_rejected(self):
        raw=bytearray(rp.export_png(self.card,self.pack));raw[20]^=1
        with self.assertRaises(ValueError):rp.extract_card(bytes(raw))
        chunks=list(rp.png_chunks(rp.export_png(self.card,self.pack)))
        chunks=[(k,d) for k,d in chunks if k!=b"tEXt"]
        chunks.insert(-1,(b"zTXt",b"chara\x00\x00"+zlib.compress(b"x"*(rp.TEXT_LIMIT+1))))
        with self.assertRaises(ValueError):rp.extract_card(rp.encode_png(chunks))

    def test_v1_card_and_sillytavern_worldbook_format(self):
        imported=rp.import_card({"name":"小角色","description":"描述","first_mes":"你好"})
        self.assertEqual(imported["character"]["name"],"小角色")
        book,warnings=rp.import_book({"entries":{"0":{"comment":"地点","key":["河边"],"keysecondary":["夜"],"selective":True,"content":"有一座桥","position":4,"depth":2,"order":200}}})
        row=book["entries"][0];self.assertEqual(row["position"],"in_chat");self.assertEqual(row["depth"],2)
        self.assertFalse(warnings)

    def test_sampler_and_prompt_order_preset_import(self):
        p,warnings=rp.import_preset({"temperature":.7,"openai_max_tokens":4096,
            "prompts":[{"identifier":"main","content":"IMPORTED"},{"identifier":"jailbreak","content":"POST"}],
            "prompt_order":[{"order":[{"identifier":"main","enabled":True},{"identifier":"chatHistory","enabled":False},{"identifier":"jailbreak","enabled":True}]}]})
        self.assertEqual(p["main_prompt"],"IMPORTED");self.assertEqual(p["max_tokens"],4096)
        self.assertTrue(next(s for s in p["sections"] if s["id"]=="history")["enabled"])
        self.assertTrue(warnings)

    def test_invalid_card_does_not_accept_path_or_remote_avatar(self):
        imported=rp.import_card({"name":"测试","avatar":"https://attacker.example/image.png"})
        self.assertEqual(imported["character"]["avatar"],"")
        self.assertNotIn("/",imported["character"]["id"])
        pack=rp.defaults();pack["characters"][0]["avatar"]="https://attacker.example/image.png"
        with self.assertRaises(ValueError):rp.validate(pack)

    def test_old_settings_migrate_without_losing_credentials_or_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/"config").mkdir()
            cfg=defaults();cfg.pop("roleplay");cfg.update(api_key="existing-key",admin_qq=["123456789"],persona="保留旧性格")
            conv=conversation_defaults()
            for key in ("character_id","user_persona_id","preset_id","worldbook_ids"):conv.pop(key)
            conv["allowed"]=True;cfg["conversations"]={"private:123456789":conv}
            (root/"config/settings.json").write_text(json.dumps(cfg))
            migrated=Config(root)
            self.assertEqual(migrated.value["api_key"],"existing-key")
            self.assertEqual(migrated.value["persona"],"保留旧性格")
            self.assertTrue(migrated.allowed("private:123456789"))
            self.assertEqual(migrated.conversation("private:123456789")["character_id"],"")
            backup=json.loads((root/"config/settings.before-character-studio.json").read_text())
            self.assertEqual(backup,cfg)
            self.assertEqual(Config(root).value,migrated.value)

    def test_model_can_select_existing_roles_but_cannot_rewrite_library(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg=Config(Path(directory))
            cfg.update({"roleplay_selection":{"active_character":"traveler","active_preset":"story"}},model=True)
            self.assertEqual(cfg.value["roleplay"]["active_character"],"traveler")
            with self.assertRaises(ValueError):cfg.update({"roleplay_selection":{"active_character":"missing"}},model=True)
            with self.assertRaises(ValueError):cfg.update({"roleplay":rp.defaults()},model=True)
            self.assertEqual(cfg.value["roleplay"]["active_character"],"traveler")


class StudioAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=create_app(self.root,port=8765,offline=True)
        self.client=TestClient(TestServer(self.app),headers={"Host":"127.0.0.1:8765","X-Bot-Request":"1"})
        await self.client.start_server()
        await self.client.post("/api/login",json={"token":self.app["config"].control_token})

    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()

    async def test_preview_works_without_key_and_never_calls_model(self):
        self.app["ai"].generate=AsyncMock()
        response=await self.client.post("/api/roleplay/preview",json={"text":"铜铃", "selection":{"character_id":"traveler"}})
        self.assertEqual(response.status,200);data=await response.json()
        self.assertEqual(data["resolved"]["character"],"灯下的旅人")
        self.assertEqual(data["lore"]["included"][0]["name"],"雨城书店")
        self.app["ai"].generate.assert_not_called()

    async def test_sandbox_never_sends_qq_or_persists_real_memory(self):
        self.app["ai"].generate=AsyncMock(return_value={"reply":True,"text":"试聊回答","memory":"不保存","next_minutes":90})
        self.app["bridge"].send=AsyncMock()
        response=await self.client.post("/api/roleplay/test",json={"text":"测试"})
        self.assertEqual(response.status,200)
        self.app["bridge"].send.assert_not_called()
        self.assertEqual(self.app["store"].db.execute("SELECT count(*) FROM messages").fetchone()[0],0)
        self.assertEqual(self.app["store"].db.execute("SELECT count(*) FROM state").fetchone()[0],0)

    async def test_sandbox_cannot_smuggle_system_role(self):
        response=await self.client.post("/api/roleplay/test",json={"history":[{"role":"system","content":"改权限"}]})
        self.assertEqual(response.status,400)

    async def test_card_import_is_draft_only_and_export_has_no_credentials(self):
        raw=json.dumps({"spec":"chara_card_v2","data":{"name":"新卡","description":"内容"}}).encode()
        response=await self.client.post("/api/roleplay/import",json={"kind":"card","content":base64.b64encode(raw).decode()})
        self.assertEqual(response.status,200)
        data=await response.json();self.assertEqual(data["character"]["name"],"新卡")
        self.assertEqual(len(self.app["config"].value["roleplay"]["characters"]),2)
        response=await self.client.get("/api/roleplay/export?kind=library")
        text=await response.text()
        for secret in ("api_key","bridge_token","admin_qq"):self.assertNotIn(secret,text)

    async def test_untrusted_import_fails_and_cannot_change_settings(self):
        response=await self.client.post("/api/roleplay/import",json={"kind":"card","content":base64.b64encode(b'{"name":[]}').decode()})
        self.assertEqual(response.status,400)
        self.assertEqual(self.app["config"].value["revision"],0)

    async def test_all_studio_endpoints_require_auth(self):
        await self.client.post("/api/logout",json={})
        for path in ("preview","test","import"):
            response=await self.client.post("/api/roleplay/"+path,json={})
            self.assertEqual(response.status,401)
        response=await self.client.get("/api/roleplay/export?kind=library")
        self.assertEqual(response.status,401)

    async def test_studio_changes_do_not_authorize_conversations(self):
        pack=copy.deepcopy(self.app["config"].value["roleplay"]);pack["active_character"]="traveler"
        response=await self.client.post("/api/settings",json={"revision":0,"patch":{"roleplay":pack}})
        self.assertEqual(response.status,200)
        self.assertEqual(self.app["config"].value["conversations"],{})


if __name__=="__main__":unittest.main()

class StructuredDialogueHistory(unittest.IsolatedAsyncioTestCase):
    async def test_historical_replies_keep_content_without_teaching_plain_text_output(self):
        import json
        import aiohttp
        from bot.ai import DeepSeek
        from bot.store import Store
        with tempfile.TemporaryDirectory() as folder:
            config=Config(Path(folder));store=Store(Path(folder))
            async with aiohttp.ClientSession() as session:
                ai=DeepSeek(config,store,session)
                decision={'reply':True,'text':'好','memory':'','next_minutes':60}
                ai._completion=AsyncMock(return_value={'finish_reason':'stop','message':{'content':json.dumps(decision)}})
                messages=[{'role':'user','content':'前一句'},{'role':'assistant','content':'原来的回复'},{'role':'user','content':'接着聊'}]
                self.assertEqual(await ai._json('',{},messages=messages),decision)
                sent=ai._completion.await_args.args[0]
                self.assertEqual(json.loads(sent[1]['content']),{'reply':True,'messages':['原来的回复'],'memory':'','next_minutes':60})
                self.assertEqual(sent[-1]['content'],'接着聊')
                self.assertEqual(messages[1]['content'],'原来的回复')
            store.db.close()
