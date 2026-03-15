"""
Minimal MCP client - used only at session start to read TD config.

Sends JSON-RPC over HTTP to Envoy's MCP endpoint, parses SSE responses.
After reading service account path and database ID, all further
communication goes through Firestore (no more MCP calls).
"""

import json
import urllib.request
import urllib.error


class McpClient:
    """Thin HTTP client for Envoy MCP JSON-RPC."""

    def __init__(self, port: int = 9870):
        self.url = f'http://localhost:{port}/mcp'
        self._id = 0

    def call(self, tool_name: str, arguments: dict) -> dict:
        """Call an MCP tool and return the parsed result."""
        self._id += 1
        message = {
            'jsonrpc': '2.0',
            'id': self._id,
            'method': 'tools/call',
            'params': {
                'name': tool_name,
                'arguments': arguments,
            },
        }
        data = json.dumps(message).encode('utf-8')
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.URLError as e:
            raise ConnectionError(
                f'Cannot reach Envoy MCP at {self.url}. '
                f'Is TouchDesigner running with Envoy enabled? ({e})'
            ) from e

        body = resp.read().decode('utf-8')

        # Parse SSE format: "event: message\ndata: {...}\n\n"
        for line in body.split('\n'):
            stripped = line.strip()
            if stripped.startswith('data: '):
                return json.loads(stripped[6:])

        # Fallback: plain JSON
        if body.strip():
            return json.loads(body)

        raise ValueError('Empty response from MCP server')

    def execute_python(self, code: str) -> str:
        """Run Python code in TD and return the result string."""
        response = self.call('execute_python', {'code': code})
        # Extract text content from MCP result
        result = response.get('result', {})
        content = result.get('content', [])
        text = ''
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    text = item.get('text', '')
                    break
        # Envoy wraps results in JSON: {"success": true, "result": "..."}
        # Extract the inner result value
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and 'result' in parsed:
                    return str(parsed['result'])
                if isinstance(parsed, dict) and 'error' in parsed:
                    raise RuntimeError(parsed['error'])
            except (json.JSONDecodeError, TypeError):
                pass
        return text

    def get_service_account_path(self) -> str:
        """Read the Privatekey parameter from the firestore COMP."""
        text = self.execute_python(
            "result = op('/gcp/firestore').par.Privatekey.eval()"
        )
        path = text.strip()
        if not path:
            raise ValueError(
                'Could not read Privatekey param from TD. '
                'Is the Firestore COMP loaded?'
            )
        return path

    def get_database_id(self) -> str:
        """Read the Databaseid parameter from the firestore COMP."""
        text = self.execute_python(
            "result = op('/gcp/firestore').par.Databaseid.eval()"
        )
        return text.strip() or '(default)'

    def is_connected(self) -> bool:
        """Check if the Firestore COMP is connected."""
        try:
            text = self.execute_python(
                "result = str(op('/gcp/firestore').ext.FirestoreExt._db is not None)"
            )
            return text.strip() == 'True'
        except Exception:
            return False
