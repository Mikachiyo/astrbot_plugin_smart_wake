"""三套 Jev questions 模板：唤醒判定 / conversation 结束与跳过判定 / 闭嘴与解除。"""

DEFAULT_WAKE_VALUE = (
    "bot此刻回复能带来多大价值？原则：技术问答价值高；礼仪祝福永远适合参与；"
    "资讯和游戏话题bot知识可能滞后，价值低；情感倾诉需要的是人类陪伴，bot回复价值低；"
    "玩梗bot容易接得生硬。若bot自己刚说过类似的话，回复价值极低；"
    "若用户正在与其他人或另一个Bot交流，回复价值极低。"
)

DEFAULT_WAKE_NATURAL = (
    "以群友身份此刻插话是否自然？若bot刚才已回复过类似内容，极不自然；"
    "若大家在热烈私聊、话题与bot无关，或用户正在与另一个Bot交流，突兀；若话题开放或气氛冷清，自然。"
)

DEFAULT_END_CONV = (
    "bot是否应彻底结束当前对话，不再主动回复？"
    "以下情况应结束：用户明确表示要结束/道别/不用了（如'行了没事了'、'一边玩去吧'、'好的'、'拜拜'等）/ "
    "对话自然完结 / 话题已彻底转变与bot无关。"
    "以下情况不应结束：对方继续向bot提问、搭话或回应bot上一次发言。"
)

DEFAULT_SKIP_REPLY = (
    "用户当前发言是否正在与其他人/另一个Bot交流（如提到别人的名字、艾特了别人、正在回应别人），"
    "或者当前不需要本bot插话，本bot应该暂时跳过本次回复并保持安静？"
)

DEFAULT_SHUTUP = "这句话是否是在要求bot（{bot_name}）停止说话/闭嘴/不要参与聊天？注意结合上下文，bot最近的回复是：{recent}"

DEFAULT_UNMUTE = "这句话是否是在允许/要求bot（{bot_name}）恢复说话/解除闭嘴？"


def build_wake_questions(
    topics: list[dict],
    hint_topic: dict | None,
    val_instructions: str = "",
    nat_instructions: str = "",
) -> dict:
    """§4.2 唤醒判定 questions。criteria 为话题表所有 key -> 中文描述。"""
    topic_criteria = {t.get("key", ""): (t.get("name") or t.get("key", ""))
                      for t in topics or [] if t.get("key")}
    hint_name = (hint_topic or {}).get("name") or "无"
    return {
        "topic": {
            "type": "choice",
            "instructions": f"当前消息属于什么话题类型？前置分析提示可能是{hint_name}，请验证。",
            "criteria": topic_criteria,
        },
        "response_value": {
            "type": "score",
            "instructions": (val_instructions or DEFAULT_WAKE_VALUE).strip(),
            "criteria": ["完全没必要回复", "可有可无", "略有价值", "比较有价值", "很有价值"],
        },
        "natural_to_join": {
            "type": "noul",
            "instructions": (nat_instructions or DEFAULT_WAKE_NATURAL).strip(),
            "criteria": {"true": "此刻插话自然", "false": "此刻插话突兀"},
        },
    }


def build_end_questions(
    match_hint: str,
    end_instructions: str = "",
    skip_instructions: str = "",
) -> dict:
    """§6 conversation 结束与跳过判定。支持判定彻底结束或暂时跳过本轮回复。"""
    base_end = (end_instructions or DEFAULT_END_CONV).strip()
    end_instr = f"{base_end} 前置关键词情报：{match_hint}"
    return {
        "end_conversation": {
            "type": "noul",
            "instructions": end_instr,
            "criteria": {"true": "应彻底结束对话", "false": "不应结束"},
        },
        "skip_reply": {
            "type": "noul",
            "instructions": (skip_instructions or DEFAULT_SKIP_REPLY).strip(),
            "criteria": {"true": "应跳过本次回复（保持安静）", "false": "无需跳过"},
        },
    }


def build_shutup_questions(bot_name: str, bot_recent: list[str], shutup_template: str = "") -> dict:
    """§7 闭嘴判定。"""
    recent = " / ".join(bot_recent) if bot_recent else "（无）"
    tpl = (shutup_template or DEFAULT_SHUTUP).strip()
    try:
        instr = tpl.format(bot_name=bot_name, recent=recent)
    except Exception:
        instr = f"{tpl}（bot称呼: {bot_name}，最近回复: {recent}）"
    return {
        "shut_up": {
            "type": "noul",
            "instructions": instr,
            "criteria": {"true": "是要求bot闭嘴", "false": "不是"},
        },
    }


def build_unmute_questions(bot_name: str, unmute_template: str = "") -> dict:
    """§7 解除闭嘴判定。"""
    tpl = (unmute_template or DEFAULT_UNMUTE).strip()
    try:
        instr = tpl.format(bot_name=bot_name)
    except Exception:
        instr = f"{tpl}（bot称呼: {bot_name}）"
    return {
        "unmute": {
            "type": "noul",
            "instructions": instr,
            "criteria": {"true": "是允许bot恢复说话", "false": "不是"},
        },
    }
