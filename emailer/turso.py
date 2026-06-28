import aiohttp


class TursoClient:
    def __init__(self, url, token):
        self.url = url
        self.token = token

    def _format_params(self, params):
        if not params:
            return []
        formatted = []
        for p in params:
            if p is None:
                formatted.append({"type": "null"})
            elif isinstance(p, bool):
                formatted.append({"type": "integer", "value": 1 if p else 0})
            elif isinstance(p, int):
                formatted.append({"type": "integer", "value": p})
            elif isinstance(p, float):
                formatted.append({"type": "real", "value": p})
            else:
                formatted.append({"type": "text", "value": str(p)})
        return formatted

    async def execute(self, sql, params=None):
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            payload = {"stmt": {"sql": sql}}
            if params:
                payload["stmt"]["args"] = self._format_params(params)
            full_url = f"{self.url}/v1/execute"
            try:
                async with session.post(full_url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise Exception(f"Turso error {resp.status}: {error}")
                    data = await resp.json()
                    if data.get("error"):
                        raise Exception(f"Turso error: {data['error']}")
                    return data
            except aiohttp.ClientError as e:
                raise Exception(f"Connection error: {e}")
