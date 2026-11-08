import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from yt_dlp import YoutubeDL

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]  # assistant (userbot) account
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")  # optional cookies.txt for YouTube

bot = Client("musicbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True)
call = PyTgCalls(assistant)

# chat_id -> list of {"title", "url", "video", "by"}
queues: dict[int, list] = {}


def _extract(query: str, video: bool) -> dict:
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": "best[height<=480]/best" if video else "bestaudio/best",
        "default_search": "ytsearch1",
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:
            info = info["entries"][0]
        return {"title": info.get("title", "Unknown"), "url": info["url"], "video": video}


async def resolve(query: str, video: bool) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract, query, video)


def make_stream(track: dict) -> MediaStream:
    if track["video"]:
        return MediaStream(
            track["url"],
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.SD_480p,
        )
    return MediaStream(
        track["url"],
        audio_parameters=AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
    )


async def start_next(chat_id: int):
    q = queues.get(chat_id)
    if not q:
        queues.pop(chat_id, None)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        return
    await call.play(chat_id, make_stream(q[0]))


async def handle_play(message: Message, video: bool):
    chat_id = message.chat.id
    if len(message.command) < 2:
        return await message.reply_text("Usage: /play song name or YouTube link")
    query = message.text.split(None, 1)[1]
    status = await message.reply_text("Searching...")
    try:
        track = await resolve(query, video)
    except Exception as e:
        return await status.edit_text(f"Could not find that song.\n{e}")
    track["by"] = message.from_user.mention if message.from_user else "Unknown"

    q = queues.setdefault(chat_id, [])
    q.append(track)
    if len(q) == 1:
        try:
            await call.play(chat_id, make_stream(track))
        except Exception as e:
            q.clear()
            return await status.edit_text(
                "Could not join voice chat. Make sure:\n"
                "1. Voice chat is started in this group\n"
                "2. Assistant account is added to the group\n\n"
                f"Error: {e}"
            )
        await status.edit_text(f"Now playing: {track['title']}\nRequested by {track['by']}")
    else:
        await status.edit_text(f"Added to queue (#{len(q) - 1}): {track['title']}")


@bot.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    await message.reply_text(
        "Music bot ready.\n\n"
        "/play name or link - play audio\n"
        "/vplay name or link - play video\n"
        "/pause /resume /skip /stop /queue"
    )


@bot.on_message(filters.command("play") & filters.group)
async def play_cmd(_, message: Message):
    await handle_play(message, video=False)


@bot.on_message(filters.command("vplay") & filters.group)
async def vplay_cmd(_, message: Message):
    await handle_play(message, video=True)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, message: Message):
    try:
        await call.pause(message.chat.id)
        await message.reply_text("Paused.")
    except Exception:
        await message.reply_text("Nothing is playing.")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, message: Message):
    try:
        await call.resume(message.chat.id)
        await message.reply_text("Resumed.")
    except Exception:
        await message.reply_text("Nothing is paused.")


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, message: Message):
    chat_id = message.chat.id
    q = queues.get(chat_id)
    if not q:
        return await message.reply_text("Nothing is playing.")
    q.pop(0)
    await start_next(chat_id)
    if queues.get(chat_id):
        await message.reply_text(f"Skipped. Now playing: {queues[chat_id][0]['title']}")
    else:
        await message.reply_text("Skipped. Queue is empty, left voice chat.")


@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, message: Message):
    chat_id = message.chat.id
    queues.pop(chat_id, None)
    try:
        await call.leave_call(chat_id)
    except Exception:
        pass
    await message.reply_text("Stopped and left voice chat.")


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, message: Message):
    q = queues.get(message.chat.id)
    if not q:
        return await message.reply_text("Queue is empty.")
    lines = [f"Now: {q[0]['title']}"]
    lines += [f"{i}. {t['title']}" for i, t in enumerate(q[1:], 1)]
    await message.reply_text("\n".join(lines))


# Auto-play next song when current one ends
try:
    _stream_end = fl.stream_end()
except TypeError:
    _stream_end = fl.stream_end


@call.on_update(_stream_end)
async def on_stream_end(_, update):
    chat_id = update.chat_id
    q = queues.get(chat_id)
    if q:
        q.pop(0)
    await start_next(chat_id)


def start_health_server():
    # Render Web Service needs an open port; harmless on Background Worker
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Music bot is running")

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def main():
    start_health_server()
    await bot.start()
    await assistant.start()
    await call.start()
    print("Music bot started")
    from pytgcalls import idle
    await idle()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
