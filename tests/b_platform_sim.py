'''
Author: xuqw xuqw@shait.com.cn
Date: 2026-08-19 10:16:17
LastEditors: xuqw xuqw@shait.com.cn
LastEditTime: 2026-08-19 11:01:13
FilePath: \nanobot\tests\b_platform_sim.py
Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
'''

import json
import sys
import urllib.request

API_URL = "http://127.0.0.1:8900/v1/chat/completions"
API_KEY = ""  # config.json 里 api.api_key 的值；本机回环测试留空即可


def build_prompt(form: dict) -> str:
    """B 平台后端唯一的"胶水代码"：表单数据 -> 提示词。"""
    config_lines = "\n".join(
        f"  - {c['key']}: {c['value']}" for c in form["configs"]
    )
    return (
        "$skill-creator 请按照以下配置项生成一个 skill 并保存：\n"
        f"- 技能名称: {form['skill_name']}\n"
        f"- 技能用途: {form['purpose']}\n"
        "- 配置项:\n"
        f"{config_lines}\n\n"
        "要求:\n"
        "1. 按 skill-creator 流程完成：初始化、编写、校验，校验通过后保存到 workspace 的 skills 目录。\n"
        "2. 保存后，列出该技能目录下的所有文件路径，并附 SKILL.md 全文。\n"
        "3. 简要说明该技能后续在什么场景会被触发。"
    )


def main() -> None:
    # ---- B 平台表单数据（用户在 B 平台 UI 上选择/填写的内容）----
    form = {
        "skill_name": "ticket-triage",
        "purpose": "客服工单文本自动分类、判断优先级，并给出回复草稿方向",
        "configs": [
            {"key": "分类维度", "value": "投诉 / 咨询 / 售后 / 其他"},
            {"key": "优先级规则", "value": "涉及支付或安全问题 P0；情绪激烈投诉 P1；普通咨询 P2"},
            {"key": "输出要求", "value": "分类结果 + 优先级 + 判断理由 + 建议回复草稿（中文）"},
            {"key": "语言", "value": "中文"},
        ],
    }

    body = {
        "session_id": "b-platform-test-001",
        "stream": False,
        "messages": [{"role": "user", "content": build_prompt(form)}],
    }

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    print("== B 平台发起请求 ==")
    print(f"POST {API_URL}")
    print(f"session_id: {body['session_id']}")
    print(f"提示词:\n{body['messages'][0]['content']}\n")
    print("等待 agent 生成中(多轮工具调用，可能需要几分钟)...", flush=True)

    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    print("\n== nanobot 返回(B 平台展示给用户) ==")
    print(result["choices"][0]["message"]["content"])
    print(f"\n[token 用量] {result.get('usage')}")

    # 顺手验证：skill 已落盘且中枢可识别
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    from pathlib import Path

    from nanobot.agent.skills import SkillsLoader

    workspace = Path.home() / ".nanobot" / "workspace"
    loader = SkillsLoader(workspace)
    available = [entry["name"] for entry in loader.list_skills()]
    print(f"\n[验证] workspace skills: {available}")
    print(
        f"[验证] {form['skill_name']} 已被中枢识别: "
        f"{form['skill_name'] in available}"
    )


if __name__ == "__main__":
    main()
