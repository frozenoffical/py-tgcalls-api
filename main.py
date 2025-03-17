import asyncio
import tempfile
import aiohttp
import requests
import os
import functools
import threading
import re
from flask import Flask, request, jsonify
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler  # For adding message handlers
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls import filters as pt_filters
from pytgcalls.types import Update
from pytgcalls.types.stream import StreamEnded

# Initialize Flask app
app = Flask(__name__)

# Download API URL
DOWNLOAD_API_URL = "https://frozen-youtube-api-search-link-ksog.onrender.com/download?url="

# Caching setup
search_cache = {}
download_cache = {}

# Global variables for the async clients (to be created in the dedicated loop)
assistant = None
py_tgcalls = None
clients_initialized = False

# Global dict to store status message IDs keyed by chat id.
stream_status_messages = {}

# Create a dedicated asyncio event loop for all async operations
tgcalls_loop = asyncio.new_event_loop()

def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Start the dedicated loop in its own daemon thread
tgcalls_thread = threading.Thread(target=start_loop, args=(tgcalls_loop,), daemon=True)
tgcalls_thread.start()

# Global list to hold pending update handlers.
pending_update_handlers = []

def delayed_on_update(filter_):
    """
    A decorator that defers registration of update handlers until py_tgcalls is ready.
    """
    def decorator(func):
        pending_update_handlers.append((filter_, func))
        return func
    return decorator

@delayed_on_update(pt_filters.stream_end())
async def stream_end_handler(_: PyTgCalls, update: StreamEnded):
    chat_id = update.chat_id
    try:
        # Leave the call first.
        await py_tgcalls.leave_call(chat_id)
        # Send a message indicating that the stream ended.
        await assistant.send_message(
            "@vcmusiclubot",
            f"Stream ended in chat id {chat_id}"
        )
    except Exception as e:
        print(f"Error leaving voice chat: {e}")

async def restart_bot():
    """
    Triggers a bot restart by calling the RENDER_DEPLOY_URL.
    """
    RENDER_DEPLOY_URL = os.getenv("RENDER_DEPLOY_URL", "https://api.render.com/deploy/srv-cv86ms9c1ekc73antbo0?key=dYquGSiBkCc")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(RENDER_DEPLOY_URL) as response:
                if response.status == 200:
                    print("Bot restart triggered successfully.")
                else:
                    print(f"Failed to trigger bot restart. Status code: {response.status}")
    except Exception as e:
        print(f"Error triggering bot restart: {e}")

async def init_clients():
    """
    Lazily creates and starts the Pyrogram client and PyTgCalls instance
    on the dedicated event loop, registers pending update handlers, and starts the clients.
    """
    global assistant, py_tgcalls, clients_initialized
    if not clients_initialized:
        assistant = Client(
            "assistant_account",
            session_string=os.environ.get("ASSISTANT_SESSION", "")
        )
        await assistant.start()
        py_tgcalls = PyTgCalls(assistant)
        await py_tgcalls.start()
        clients_initialized = True
        # Register all pending update handlers now that py_tgcalls is initialized.
        for filter_, handler in pending_update_handlers:
            py_tgcalls.on_update(filter_)(handler)

async def download_audio(url):
    """Downloads the audio from a given URL and returns the file path."""
    if url in download_cache:
        return download_cache[url]
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
        file_name = temp_file.name
        download_url = f"{DOWNLOAD_API_URL}{url}"
        async with aiohttp.ClientSession() as session:
            async with session.get(download_url) as response:
                if response.status == 200:
                    with open(file_name, 'wb') as f:
                        f.write(await response.read())
                    download_cache[url] = file_name
                    return file_name
                else:
                    raise Exception(f"Failed to download audio. HTTP status: {response.status}")
    except Exception as e:
        raise Exception(f"Error downloading audio: {e}")

@functools.lru_cache(maxsize=100)
def search_video(title):
    """Searches for a video using the external API and caches the result."""
    search_response = requests.get(f"https://odd-block-a945.tenopno.workers.dev/search?title={title}")
    if search_response.status_code != 200:
        return None
    return search_response.json()

async def play_media(chat_id, video_url, title):
    """Downloads and plays the media in the specified chat."""
    media_path = await download_audio(video_url)
    await py_tgcalls.play(
        chat_id,
        MediaStream(
            media_path,
            video_flags=MediaStream.Flags.IGNORE,
        )
    )

@app.route('/play', methods=['GET'])
def play():
    chatid = request.args.get('chatid')
    title = request.args.get('title')
    if not chatid or not title:
        return jsonify({'error': 'Missing chatid or title parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    search_result = search_video(title)
    if not search_result:
        return jsonify({'error': 'Failed to search video'}), 500
    video_url = search_result.get("link")
    video_title = search_result.get("title")
    if not video_url:
        return jsonify({'error': 'No video found'}), 404

    try:
        # Initialize the clients on the dedicated loop if needed.
        asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
        # Schedule play_media on the dedicated loop.
        asyncio.run_coroutine_threadsafe(play_media(chat_id, video_url, video_title), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Playing media', 'chatid': chatid, 'title': video_title})

@app.route('/stop', methods=['GET'])
def stop():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()

        async def leave_call_wrapper(cid):
            await asyncio.sleep(0)
            return await py_tgcalls.leave_call(cid)

        asyncio.run_coroutine_threadsafe(leave_call_wrapper(chat_id), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Stopped media', 'chatid': chatid})

# New /join endpoint to invite assistant to a chat.
@app.route('/join', methods=['GET'])
def join_endpoint():
    chat = request.args.get('chat')
    if not chat:
        return jsonify({'error': 'Missing chat parameter'}), 400

    # Validate and process the input similarly to the pyrogram join command.
    if re.match(r"https://t\.me/[\w_]+/?", chat):
        chat = chat.split("https://t.me/")[1].strip("/")
    elif chat.startswith("@"):
        chat = chat[1:]

    try:
        # Initialize the clients on the dedicated loop if needed.
        asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()

        async def join_chat():
            await assistant.join_chat(chat)
        asyncio.run_coroutine_threadsafe(join_chat(), tgcalls_loop).result()
    except Exception as error:
        error_message = str(error)
        if "USERNAME_INVALID" in error_message:
            return jsonify({'error': 'Invalid username or link. Please check and try again.'}), 400
        elif "INVITE_HASH_INVALID" in error_message:
            return jsonify({'error': 'Invalid invite link. Please verify and try again.'}), 400
        elif "USER_ALREADY_PARTICIPANT" in error_message:
            return jsonify({'message': f"You are already a member of {chat}."}), 200
        else:
            return jsonify({'error': error_message}), 500

    return jsonify({'message': f"Successfully Joined Group/Channel: {chat}"})

# New /pause endpoint to pause the media stream.
@app.route('/pause', methods=['GET'])
def pause():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()

        async def pause_call(cid):
            return await py_tgcalls.pause_stream(cid)
        asyncio.run_coroutine_threadsafe(pause_call(chat_id), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Paused media', 'chatid': chatid})

# New /resume endpoint to resume the paused media stream.
@app.route('/resume', methods=['GET'])
def resume():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()

        async def resume_call(cid):
            return await py_tgcalls.resume_stream(cid)
        asyncio.run_coroutine_threadsafe(resume_call(chat_id), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Resumed media', 'chatid': chatid})

if __name__ == '__main__':
    # Optionally initialize the clients at startup.
    asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)






