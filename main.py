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

# Define multiple Download API
DOWNLOAD_APIS = {
    'default': os.environ.get(
        'DOWNLOAD_API_URL',
        'https://ytapiii-8bced95bfad6.herokuapp.com/download?url='
    ),
    'secondary': os.environ.get(
        'SECONDARY_DOWNLOAD_API_URL',
        'https://polite-tilly-vibeshiftbotss-a46821c0.koyeb.app/download?url='
    ),
    'tertiary': os.environ.get(
        'TERTIARY_DOWNLOAD_API_URL',
        'https://yt-api-pvt.vercel.app/api/down?url='
    )
}

# Caching setup for downloads
download_cache = {}

# Globals for the Telegram clients
assistant = None
py_tgcalls = None
clients_initialized = False

# Dedicated loop & thread for PyTgCalls
tgcalls_loop = asyncio.new_event_loop()
def start_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()
tgcalls_thread = threading.Thread(
    target=start_loop, args=(tgcalls_loop,), daemon=True
)
tgcalls_thread.start()

# Collect handlers before init
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

# Initialize Pyrogram + PyTgCalls once
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

# Download helper: ONLY use the single API key requested
async def download_audio(url: str, api_name: str) -> str:
    if api_name not in DOWNLOAD_APIS:
        raise ValueError(
            f"Unknown API key: {api_name!r}. "
            f"Choose from {list(DOWNLOAD_APIS.keys())}."
        )
    api_base = DOWNLOAD_APIS[api_name]
    cache_key = url
    if cache_key in download_cache:
        return download_cache[cache_key]

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
    file_name = temp_file.name
    download_url = f"{api_base}{url}"
    timeout = aiohttp.ClientTimeout(total=90)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(download_url) as response:
                response.raise_for_status()
                with open(file_name, 'wb') as f:
                    f.write(await response.read())

        download_cache[cache_key] = file_name
        return file_name
    except Exception as e:
        raise Exception(f"Download via '{api_name}' failed: {e}")

# Play helper: pass api_name, not a URL
async def play_media(chat_id: int, video_url: str, api_name: str):
    media_path = await download_audio(video_url, api_name)
    await py_tgcalls.play(
        chat_id,
        MediaStream(
            media_path,
            video_flags=MediaStream.Flags.IGNORE,
        )
    )

# /play endpoint
@app.route('/play', methods=['GET'])
def play():
    chatid    = request.args.get('chatid')
    video_url = request.args.get('url')
    # If 'api' is missing or blank, this defaults to '1'
    api_param = request.args.get('api') or '1'

    # 1) Validate required params
    if not chatid or not video_url:
        return jsonify({'error': 'Missing chatid or url parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    # 2) Map numeric selector → DOWNLOAD_APIS key
    api_map = {'1': 'default', '2': 'secondary', '3': 'tertiary'}
    if api_param not in api_map:
        return jsonify({
            'error': f"Invalid api '{api_param}'. Choose 1, 2 or 3."
        }), 400
    api_key = api_map[api_param]

    # 3) Initialize clients and start playback
    try:
        asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
        asyncio.run_coroutine_threadsafe(
            play_media(chat_id, video_url, api_key),
            tgcalls_loop
        ).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'message':      'Playing media',
        'chatid':       chatid,
        'url':          video_url,
        'api_selected': api_param
    })


# /cache endpoint
@app.route('/cache', methods=['GET'])
def cache_song():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    errors = {}

    # 1) Try default API
    try:
        file_path = asyncio.run_coroutine_threadsafe(
            download_audio(url, 'default'),
            tgcalls_loop
        ).result()
        return jsonify({
            'message':    'Song cached successfully',
            'url':        url,
            'api_used':   'default'
        })
    except Exception as e:
        errors['default'] = str(e)

    # 2) Fallback to secondary API
    try:
        file_path = asyncio.run_coroutine_threadsafe(
            download_audio(url, 'secondary'),
            tgcalls_loop
        ).result()
        return jsonify({
            'message':    'Song cached successfully',
            'url':        url,
            'api_used':   'secondary'
        })
    except Exception as e:
        errors['secondary'] = str(e)

    # 3) Both failed
    return jsonify({
        'error':   'Download failed on both default and secondary APIs',
        'details': errors
    }), 500

# (Your other endpoints: /stop, /join, /pause, /resume remain unchanged)

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
            asyncio.run_coroutine_threadsafe(
                init_clients(), tgcalls_loop
            ).result()
        async def leave_call_wrapper(cid):
            await asyncio.sleep(0)
            return await py_tgcalls.leave_call(cid)
        asyncio.run_coroutine_threadsafe(
            leave_call_wrapper(chat_id), tgcalls_loop
        ).result()
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
        asyncio.run_coroutine_threadsafe(
            init_clients(), tgcalls_loop
        ).result()
        async def join_chat():
            await assistant.join_chat(chat)
        asyncio.run_coroutine_threadsafe(
            join_chat(), tgcalls_loop
        ).result()
    except Exception as error:
        err = str(error)
        if "USERNAME_INVALID" in err:
            return jsonify({'error': 'Invalid username or link.'}), 400
        elif "INVITE_HASH_INVALID" in err:
            return jsonify({'error': 'Invalid invite link.'}), 400
        elif "USER_ALREADY_PARTICIPANT" in err:
            return jsonify({
                'message': f"You are already a member of {chat}."
            }), 200
        else:
            return jsonify({'error': err}), 500

    return jsonify({'message': f"Successfully Joined: {chat}"})

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
            asyncio.run_coroutine_threadsafe(
                init_clients(), tgcalls_loop
            ).result()
        async def pause_call(cid):
            return await py_tgcalls.pause(cid)
        asyncio.run_coroutine_threadsafe(
            pause_call(chat_id), tgcalls_loop
        ).result()
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
            asyncio.run_coroutine_threadsafe(
                init_clients(), tgcalls_loop
            ).result()
        async def resume_call(cid):
            return await py_tgcalls.resume(cid)
        asyncio.run_coroutine_threadsafe(
            resume_call(chat_id), tgcalls_loop
        ).result()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Resumed media', 'chatid': chatid})

if __name__ == '__main__':
    # Ensure clients are initialized before serving
    asyncio.run_coroutine_threadsafe(init_clients(), tgcalls_loop).result()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
