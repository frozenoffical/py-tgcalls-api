import asyncio
import tempfile
import aiohttp
import os
import threading
import re
from flask import Flask, request, jsonify
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls import filters as pt_filters
from pytgcalls.types.stream import StreamEnded

# Initialize Flask app
app = Flask(__name__)

# Define multiple Download APIs
DOWNLOAD_APIS = {
    'default': os.environ.get('DOWNLOAD_API_URL', 'https://frozen-youtube-api-search-link-b89x.onrender.com/download?url='),
    'secondary': os.environ.get('SECONDARY_DOWNLOAD_API_URL', 'https://polite-tilly-vibeshiftbotss-a46821c0.koyeb.app/download?url='),
    'tertiary': os.environ.get('TERTIARY_DOWNLOAD_API_URL', 'https://frozen-youtube-api-search-link-b89x.onrender.com/download?url=')
}

# Caching setup for downloads
download_cache = {}

# Global variables for the async clients (to be created in the dedicated loop)
assistant = None
py_tgcalls = None
clients_initialized = False

tgcalls_loop = asyncio.new_event_loop()

def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

tgcalls_thread = threading.Thread(target=start_loop, args=(tgcalls_loop,), daemon=True)

tgcalls_thread.start()

pending_update_handlers = []

def delayed_on_update(filter_):
    def decorator(func):
        pending_update_handlers.append((filter_, func))
        return func
    return decorator

@delayed_on_update(pt_filters.stream_end())
async def stream_end_handler(_: PyTgCalls, update: StreamEnded):
    chat_id = update.chat_id
    try:
        await py_tgcalls.leave_call(chat_id)
        await assistant.send_message(
            "@vcmusiclubot",
            f"Stream ended in chat id {chat_id}"
        )
    except Exception as e:
        print(f"Error leaving voice chat: {e}")

async def init_clients():
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
        for filter_, handler in pending_update_handlers:
            py_tgcalls.on_update(filter_)(handler)

async def download_audio(url: str, api_name: str) -> str:
    # build ordered list of bases to try:
    bases = [
        DOWNLOAD_APIS.get(api_name, DOWNLOAD_APIS['default']),
        DOWNLOAD_APIS['secondary'],
        DOWNLOAD_APIS['tertiary'],
    ]
    cache_key = f"{url}"
    if cache_key in download_cache:
        return download_cache[cache_key]

    last_error = None
    for api_base in bases:
        try:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
            file_name = temp_file.name
            download_url = f"{api_base}{url}"
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(download_url) as response:
                    response.raise_for_status()
                    with open(file_name, 'wb') as f:
                        f.write(await response.read())
            download_cache[cache_key] = file_name
            return file_name
        except Exception as e:
            last_error = e
            # log and try next API
            print(f"[download_audio] failed with {api_base}: {e}")

    # if we get here, all APIs failed
    raise Exception(f"All download APIs failed. Last error: {last_error}")

async def play_media(chat_id: int, video_url: str, api_name: str):
    api_base = DOWNLOAD_APIS.get(api_name, DOWNLOAD_APIS['default'])
    media_path = await download_audio(video_url, api_base)
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
    video_url = request.args.get('url')
    api_name = request.args.get('api', 'default')
    if not chatid or not video_url:
        return jsonify({'error': 'Missing chatid or url parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    if api_name not in DOWNLOAD_APIS:
        return jsonify({'error': f"Invalid api parameter. Choose from {list(DOWNLOAD_APIS.keys())}"}), 400

    try:
        asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
        asyncio.run_coroutine_threadsafe(play_media(chat_id, video_url, api_name), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Playing media', 'chatid': chatid, 'url': video_url, 'api': api_name})

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

@app.route('/join', methods=['GET'])
def join_endpoint():
    chat = request.args.get('chat')
    if not chat:
        return jsonify({'error': 'Missing chat parameter'}), 400

    if re.match(r"https://t\.me/[\w_]+/?", chat):
        chat = chat.split("https://t.me/")[1].strip("/")
    elif chat.startswith("@"):
        chat = chat[1:]

    try:
        asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
        async def join_chat():
            await assistant.join_chat(chat)
        asyncio.run_coroutine_threadsafe(join_chat(), tgcalls_loop).result()
    except Exception as error:
        err = str(error)
        if "USERNAME_INVALID" in err:
            return jsonify({'error': 'Invalid username or link. Please check and try again.'}), 400
        elif "INVITE_HASH_INVALID" in err:
            return jsonify({'error': 'Invalid invite link. Please verify and try again.'}), 400
        elif "USER_ALREADY_PARTICIPANT" in err:
            return jsonify({'message': f"You are already a member of {chat}."}), 200
        else:
            return jsonify({'error': err}), 500

    return jsonify({'message': f"Successfully Joined Group/Channel: {chat}"})

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
            return await py_tgcalls.pause(cid)
        asyncio.run_coroutine_threadsafe(pause_call(chat_id), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Paused media', 'chatid': chatid})

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
            return await py_tgcalls.resume(cid)
        asyncio.run_coroutine_threadsafe(resume_call(chat_id), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Resumed media', 'chatid': chatid})

# New endpoint: cache a song for faster future playback
@app.route('/cache', methods=['GET'])
def cache_song():
    url = request.args.get('url')
    api_name = request.args.get('api', 'default')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    if api_name not in DOWNLOAD_APIS:
        return jsonify({'error': f"Invalid api parameter. Choose from {list(DOWNLOAD_APIS.keys())}"}), 400
    try:
        api_base = DOWNLOAD_APIS[api_name]
        file_path = asyncio.run_coroutine_threadsafe(download_audio(url, api_base), tgcalls_loop).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'message': 'Song cached successfully', 'url': url, 'api': api_name})

if __name__ == '__main__':
    asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)











