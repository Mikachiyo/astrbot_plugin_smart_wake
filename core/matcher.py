"""关键词话题匹配与第一级抽签（§4.1）。

话题表条目字段：key / name / words / score(0-10) / cooldown_seconds / is_question
"""

import random
import time

# 出厂预设话题表
DEFAULT_TOPICS: list[dict] = [
    {
        "key": "tech_question", "name": "技术问答", "score": 7, "cooldown_seconds": 0,
        "is_question": True,
        "words": ["报错", "错误", "配置", "安装", "卸载", "代码", "调试", "bug", "闪退", "卡住",
                  "打不开", "连不上", "服务器", "数据库", "网络", "linux", "windows", "docker",
                  "git", "python", "java", "api", "框架", "编译", "依赖", "nginx", "mysql",
                  "502", "404", "崩溃"],
    },
    {
        "key": "welcome_courtesy", "name": "礼仪问候", "score": 8, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["欢迎", "新人", "进群", "早上好", "早安", "晚安", "谢谢", "感谢", "辛苦了",
                  "恭喜", "周末愉快"],
    },
    {
        "key": "festival_blessing", "name": "节日祝福", "score": 6, "cooldown_seconds": 86400,
        "is_question": False,
        "words": ["春节", "新年", "元旦", "元宵", "端午", "中秋", "国庆", "圣诞", "生日", "节日"],
    },
    {
        "key": "daily_chat", "name": "日常闲聊", "score": 4, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["吃饭", "外卖", "食堂", "猫", "狗", "天气", "下雨", "电影", "追剧", "上班",
                  "下班", "加班", "摸鱼", "周末", "放假", "快递", "减肥", "健身"],
    },
    {
        "key": "knowledge_share", "name": "资讯分享", "score": 2, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["新闻", "热搜", "官宣", "发布会", "爆料", "震惊", "刚刚看到", "据说"],
    },
    {
        "key": "game_play", "name": "游戏娱乐", "score": 2, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["开黑", "排位", "段位", "抽卡", "氪金", "副本", "攻略", "皮肤", "原神",
                  "王者", "lol", "steam", "削弱", "加强", "版本更新"],
    },
    {
        "key": "meme_play", "name": "玩梗灌水", "score": 1, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["草", "笑死", "绷不住", "抽象", "典", "急", "孝", "乐", "yyds", "绝绝子",
                  "栓q", "破防"],
    },
    {
        "key": "emotional", "name": "情感倾诉", "score": 1, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["失恋", "难过", "难受", "焦虑", "失眠", "emo", "压力", "想哭", "抑郁",
                  "心烦", "崩溃", "想死"],
    },
    {
        "key": "transaction", "name": "事务广告", "score": 0, "cooldown_seconds": 0,
        "is_question": False,
        "words": ["出二手", "拼单", "接龙", "代购", "转让", "闲置", "广告", "加微信", "刷单"],
    },
]


def match_topics(text: str, topics: list[dict]) -> list[dict]:
    """返回命中的话题列表（子串匹配，大小写不敏感）。score=0 也命中（等同过滤，由抽签处处理）。"""
    text_l = (text or "").lower()
    hits = []
    for t in topics or []:
        words = [w for w in (t.get("words") or []) if isinstance(w, str) and w]
        if any(w.lower() in text_l for w in words):
            hits.append(t)
    return hits


def first_level_draw(
    text: str,
    topics: list[dict],
    cooldowns: dict,
    fallback_probability: int,
    now: float | None = None,
) -> dict:
    """第一级抽签。返回 {
        passed, topic(命中话题dict或None), hits(各话题抽签明细), fallback(兜底骰信息)
    }。冷却中的话题视为未命中。"""
    now = now if now is not None else time.time()
    hits = []
    passed_topics = []
    for t in match_topics(text, topics):
        key = t.get("key", "")
        cd_until = float((cooldowns or {}).get(key, 0) or 0)
        if cd_until > now:
            hits.append({"key": key, "name": t.get("name"), "score": t.get("score"),
                         "roll": None, "passed": False, "cooling": True})
            continue
        score = int(t.get("score", 0) or 0)
        if score <= 0:
            # score=0 等同过滤：命中也必不通过
            hits.append({"key": key, "name": t.get("name"), "score": 0,
                         "roll": None, "passed": False, "cooling": False})
            continue
        roll = random.randint(1, 100)
        ok = roll <= score * 10
        hits.append({"key": key, "name": t.get("name"), "score": score,
                     "roll": roll, "passed": ok, "cooling": False})
        if ok:
            passed_topics.append(t)
    if passed_topics:
        # 多个通过时取分值最高的话题
        best = max(passed_topics, key=lambda t: int(t.get("score", 0) or 0))
        return {"passed": True, "topic": best, "hits": hits, "fallback": None}
    # 零命中/全未过 → 兜底骰
    fb_roll = random.randint(1, 100)
    fb_ok = fb_roll <= int(fallback_probability or 0)
    return {
        "passed": fb_ok,
        "topic": None,
        "hits": hits,
        "fallback": {"roll": fb_roll, "threshold": int(fallback_probability or 0), "passed": fb_ok},
    }


def hit_must_wake(text: str, must_wake_words: list[str]) -> str | None:
    """命中必唤醒词则返回该词。"""
    text_l = (text or "").lower()
    for w in must_wake_words or []:
        if isinstance(w, str) and w and w.lower() in text_l:
            return w
    return None
