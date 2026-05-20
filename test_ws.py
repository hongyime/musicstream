import asyncio
import websockets
import json

async def test_ws():
    uri = "ws://127.0.0.1:9079/ws/health"
    async with websockets.connect(uri) as websocket:
        print("Connected to WebSocket")
        # Wait for first broadcast
        message = await websocket.recv()
        print(f"Received: {message}")
        data = json.loads(message)
        assert isinstance(data, list)
        print("WebSocket check passed!")

if __name__ == "__main__":
    asyncio.run(test_ws())
