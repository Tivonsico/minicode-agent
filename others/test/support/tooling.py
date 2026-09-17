import json


class Outputs:
    def __init__(self, root):
        self.root = root
        self.directory = root / ".minicode" / "sessions" / "test" / "outputs"
        self.directory.mkdir(parents=True)
        self.allowed = set()
        self.records = []

    def allowed_paths(self):
        return self.allowed

    def save(self, call_id, text, stream="output"):
        path = self.directory / f"{call_id}.{stream}.txt"
        path.write_text(text, encoding="utf-8")
        self.allowed.add(path.resolve())
        record = {
            "path": str(path.relative_to(self.root)),
            "ref": "outputs/" + path.name,
            "chars": len(text),
        }
        self.records.append(record)
        return record

    def preview(self, call_id, text, stream="output", limit=16000):
        if len(text) <= limit:
            return text
        record = self.save(call_id, text, stream)
        return text[:100] + " ReadFile: " + record["path"]


def call(name, args, cid="call"):
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
