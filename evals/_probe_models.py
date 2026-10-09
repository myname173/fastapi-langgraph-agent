import os, sys, io  # noqa: E401

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from openai import OpenAI  # noqa: E402  (must import after sys.path setup)

key = (
    open(os.path.join(os.path.dirname(__file__), "..", "secrets", "embedding_api_key.txt"), encoding="utf-8")
    .read()
    .strip()
)
base = "https://ws-jsbo0s00m9tnj2xv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
candidates = ["qwen3.7-flash", "qwen3.7-max-preview", "glm-5.2", "qwen3.5-ocr"]
client = OpenAI(api_key=key, base_url=base)
for m in candidates:
    try:
        r = client.chat.completions.create(
            model=m, messages=[{"role": "user", "content": "回复一个字：好"}], max_tokens=16
        )
        print(f"OK   {m} -> {r.choices[0].message.content!r}", flush=True)
    except Exception as e:
        msg = str(e)
        code = "403" if "403" in msg else ("429" if "429" in msg else "ERR")
        short = msg.split("}")[0][:140] if "}" in msg else msg[:140]
        print(f"{code}  {m} -> {short}", flush=True)
