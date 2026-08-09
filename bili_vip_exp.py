# -*- coding: utf-8 -*-
"""
B站大会员「每日看视频1分钟领10经验」自动领取脚本（GitHub Actions / 阿里云FC 通用版）

原理：
  1. 上报一次视频观看心跳（模拟看视频，满足"看视频1分钟"条件）
  2. 调用大会员每日经验领取接口领取 10 经验

接口依据（B站官方 API 文档 bilibili-API-collect）：
  领取：POST https://api.bilibili.com/x/vip/experience/add
        成功 code=0；已领取 code=69198（重复运行安全，不会重复发经验）

运行方式：
  1) 本地调试：    python bili_vip_exp.py   （读取 config.json）
  2) GitHub Actions：由 .github/workflows/*.yml 定时运行，配置放仓库 Secrets
  3) 阿里云FC：    入口 handler，配置放环境变量

环境变量：
  SESSDATA / BILI_JCT     必填，B站登录凭证
  NOTIFY_TYPE             none | serverchan | dingtalk | pushplus（默认 none 不推送）
  SERVERCHAN_KEY          Server酱 SendKey（微信推送）
  DINGTALK_WEBHOOK        钉钉自定义机器人 webhook（含 access_token）
  PUSHPLUS_TOKEN          PushPlus token（微信推送）
  NOTIFY_ON_SUCCESS       true/false，是否在领取成功时也推送（默认 false）
"""

import json
import os
import random
import time
import urllib.parse
import urllib.request

# 本地调试时读取的配置文件（云端用环境变量，不用此文件）
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ---------- 基础 HTTP 请求（只用标准库，云端无需装依赖） ----------

def _headers(cookie):
    return {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    }


def http_get(url, cookie):
    req = urllib.request.Request(url, headers=_headers(cookie))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_form(url, data, cookie):
    body = urllib.parse.urlencode(data).encode("utf-8")
    h = _headers(cookie)
    h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get_raw(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def _http_post_json(url, obj, headers):
    body = json.dumps(obj).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


# ---------- 配置读取：云端优先环境变量，本地回退 config.json ----------

def load_config():
    sessdata = os.environ.get("SESSDATA", "").strip()
    bili_jct = os.environ.get("BILI_JCT", "").strip()

    if not sessdata or not bili_jct:
        # 没有环境变量时，尝试本地 config.json，方便本地调试
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            sessdata = str(cfg.get("sessdata", "")).strip()
            bili_jct = str(cfg.get("bili_jct", "")).strip()
        else:
            print("[错误] 未配置 SESSDATA / BILI_JCT 环境变量，且找不到 config.json。")
            raise SystemExit(1)

    if not sessdata or "替换" in sessdata or "SESSDATA" in sessdata:
        raise SystemExit("SESSDATA 未正确填写！")
    if not bili_jct or "替换" in bili_jct or "bili_jct" in bili_jct:
        raise SystemExit("BILI_JCT 未正确填写！")

    return f"SESSDATA={sessdata}; bili_jct={bili_jct}", bili_jct


# ---------- B站领取逻辑 ----------

def get_random_video(cookie):
    """从热门视频里随机取一个，用于上报观看心跳。失败返回 (None, None)。"""
    try:
        j = http_get("https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1", cookie)
        if j.get("code") == 0 and j.get("data", {}).get("list"):
            v = random.choice(j["data"]["list"])
            return v.get("bvid"), v.get("cid")
    except Exception as e:
        print(f"[注意] 获取热门视频失败（不影响领取）：{e}")
    return None, None


def report_watch(bvid, cid, csrf, cookie):
    """上报视频观看心跳，模拟"看视频1分钟"。"""
    if not bvid or not cid:
        return
    played = 75  # 播放秒数，>60 视为满 1 分钟
    data = {
        "bvid": bvid,
        "cid": cid,
        "id": cid,
        "platform": "web",
        "realtime": str(played),
        "played_time": str(played),
        "type": "3",
        "start_ts": str(int(time.time())),
        "csrf": csrf,
    }
    try:
        j = http_post_form("https://api.bilibili.com/x/click-interface/web/heartbeat", data, cookie)
        print(f"[观看] 已上报心跳：{bvid}（{played}秒） 返回code={j.get('code')}")
    except Exception as e:
        print(f"[注意] 上报观看心跳失败（不影响领取）：{e}")


def claim_vip_exp(csrf, cookie):
    j = http_post_form("https://api.bilibili.com/x/vip/experience/add", {"csrf": csrf}, cookie)
    return j.get("code"), j.get("message"), j.get("data", {})


# ---------- 通知推送（cookie 过期等告警） ----------

def notify(title, content):
    """按 NOTIFY_TYPE 推送告警。失败仅打印，不影响主流程。"""
    ntype = os.environ.get("NOTIFY_TYPE", "none").strip().lower()
    try:
        if ntype == "serverchan":
            key = os.environ.get("SERVERCHAN_KEY", "").strip()
            if not key:
                print("[通知] 未配置 SERVERCHAN_KEY"); return
            url = ("https://sctapi.ftqq.com/" + key + ".send?title="
                   + urllib.parse.quote(title) + "&desp=" + urllib.parse.quote(content))
            _http_get_raw(url, {"User-Agent": USER_AGENT})
            print("[通知] 已通过 Server酱（微信）推送")

        elif ntype == "dingtalk":
            token = os.environ.get("DINGTALK_WEBHOOK", "").strip()
            if not token:
                print("[通知] 未配置 DINGTALK_WEBHOOK"); return
            url = "https://oapi.dingtalk.com/robot/send?access_token=" + token
            # 钉钉机器人设置了自定义关键词「GZY」，消息里必须包含该词才会放行
            _http_post_json(url, {"msgtype": "text", "text": {"content": "【GZY】" + title + "\n" + content}},
                            {"User-Agent": USER_AGENT})
            print("[通知] 已通过钉钉机器人推送")

        elif ntype == "pushplus":
            token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
            if not token:
                print("[通知] 未配置 PUSHPLUS_TOKEN"); return
            _http_post_json("http://www.pushplus.plus/send",
                            {"token": token, "title": title, "content": content},
                            {"User-Agent": USER_AGENT})
            print("[通知] 已通过 PushPlus（微信）推送")

        else:
            print("[通知] NOTIFY_TYPE 未配置或为 none，本次不推送")
    except Exception as e:
        print(f"[通知] 推送失败：{e}")


def notify_on_success():
    return os.environ.get("NOTIFY_ON_SUCCESS", "false").strip().lower() == "true"


# ---------- 主流程 ----------

def main():
    cookie, csrf = load_config()

    # 1) 模拟观看
    bvid, cid = get_random_video(cookie)
    report_watch(bvid, cid, csrf, cookie)
    time.sleep(1)

    # 2) 领取大会员每日经验
    code, message, data = claim_vip_exp(csrf, cookie)

    if code == 0:
        print("[结果] 领取成功！今日大会员 +10 经验。")
        if notify_on_success():
            notify("B站今日经验已领取", "大会员每日看视频经验 +10 已到账。")
    elif code == 69198:
        print("[结果] 今天已经领取过了（不会重复发放），无需担心。")
    elif code == -101:
        msg = "B站 cookie 已失效（SESSDATA 过期/登录态掉了），请到阿里云 FC 控制台更新环境变量 SESSDATA！"
        print("[结果] " + msg)
        notify("B站 cookie 过期，需更新", msg)
    elif code == -111:
        msg = "B站 csrf 校验失败，请更新环境变量 BILI_JCT！"
        print("[结果] " + msg)
        notify("B站 csrf 校验失败", msg)
    elif code == 6034007:
        print("[结果] 请求过于频繁，请稍后再试。")
    else:
        msg = f"领取返回 code={code}，message={message}，data={data}"
        print("[结果] " + msg)
        notify("B站经验领取异常", msg)


# 阿里云函数计算入口
def handler(event, context):
    main()
    return "ok"


if __name__ == "__main__":
    main()