import json
import os
import tempfile
from urllib.parse import urlparse

ROOT = os.path.join(os.path.dirname(__file__), "data")

def read(name):
    try:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def write(name, value):
    os.makedirs(ROOT, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=ROOT, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, os.path.join(ROOT, name))
    finally:
        if os.path.exists(temp): os.unlink(temp)

def public():
    model = read("llm_config.json")
    models = model.get("models") or {}
    return {"base_url": model.get("base_url", ""), "default_model": model.get("default_model", ""),
            "title_translation": models.get("title_translation", ""), "summarize": models.get("summarize", ""),
            "key_configured": bool(model.get("api_key")), "authorization_configured": bool(read("reduct_config.json").get("authorization"))}

def save(body):
    if body.get("kind") == "model":
        cfg = read("llm_config.json")
        base = str(body.get("base_url", "")).strip().rstrip("/")
        model = str(body.get("summarize", "")).strip()
        translation = str(body.get("title_translation", "")).strip()
        if urlparse(base).scheme not in ("http", "https") or not urlparse(base).hostname or not model or not translation:
            raise ValueError("请填写有效服务地址并选择翻译、总结模型")
        cfg.update(base_url=base)
        models = cfg.setdefault("models", {})
        for field in ("title_translation", "summarize"):
            models[field] = str(body.get(field, "")).strip() or model
        key = str(body.get("api_key", "")).strip()
        if key: cfg["api_key"] = key
        write("llm_config.json", cfg)
    elif body.get("kind") == "reduct":
        cfg = read("reduct_config.json")
        authorization = str(body.get("authorization", "")).strip()
        if "\n" in authorization or "\r" in authorization: raise ValueError("Authorization 必须为单行内容")
        if authorization.lower().startswith("authorization:"): raise ValueError("请只粘贴 Authorization 的值")
        cfg.pop("cookie", None)
        if body.get("clear"): cfg.pop("authorization", None)
        elif authorization: cfg["authorization"] = authorization
        write("reduct_config.json", cfg)
    else:
        raise ValueError("未知设置类型")
    return {"ok": True}


def list_models(body):
    import urllib.request
    import urllib.error
    cfg = read("llm_config.json")
    base = str(body.get("base_url") or cfg.get("base_url", "")).strip().rstrip("/")
    key = str(body.get("api_key") or "").strip()
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("请填写有效 API 服务地址")
    if not key:
        if parsed.netloc != urlparse(cfg.get("base_url", "")).netloc:
            raise ValueError("更换服务地址后，请输入对应的 API Key 再获取模型")
        key = cfg.get("api_key", "")
    if not key: raise ValueError("请先输入 API Key")
    req = urllib.request.Request(base + "/models", headers={"Authorization":"Bearer " + key, "Accept":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code in (401,403): raise ValueError("API Key 无效或没有获取模型列表的权限") from None
        if e.code == 404: raise ValueError("此 API 地址未提供 /models 接口，请检查服务地址") from None
        raise ValueError("获取模型列表失败（HTTP %s）" % e.code) from None
    except Exception:
        raise ValueError("模型列表暂时无法获取，请检查服务地址和网络") from None
    models = sorted(set(str(m["id"]) for m in payload.get("data", []) if isinstance(m,dict) and m.get("id")))
    if not models: raise ValueError("服务返回的模型列表为空")
    return {"ok":True, "models":models}
