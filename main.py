import asyncio
import tempfile
import aiohttp
import os
import re
import sys
from quart import Quart, request, jsonify
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls import filters as pt_filters
from pytgcalls.types.stream import StreamEnded

# Initialize Quart app (High concurrency async replacement for Flask)
app = Quart(__name__)

DOWNLOAD_APIS = {
    'default': os.environ.get(
        'DOWNLOAD_API_URL',
        'https://divine-dream-fde5.lagendplayersyt.workers.dev/down?url='
    ),
    'secondary': os.environ.get(
        'SECONDARY_DOWNLOAD_API_URL',
        'https://frozen-youtube-api-search-link-b89x.onrender.com/download?url='
    ),
    'tertiary': os.environ.get(
        'TERTIARY_DOWNLOAD_API_URL',
        'https://ytapi-df6f5442e070.herokuapp.com/download?url='
    )
}

# Caching setup for downloads
download_cache = {}
# Lock to prevent simultaneous downloads of the same URL
download_locks = {}

# Globals for the Telegram clients
assistant = None
py_tgcalls = None
clients_initialized = False

# Temporary storage for handlers before client init
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
        # Using the global assistant client to send the message
        await assistant.send_message(
            "@vcmusiclubot",
            f"Stream ended in chat id {chat_id}"
        )
    except Exception as e:
        print(f"Error leaving voice chat: {e}")

# Lifecycle: Initialize Clients on Startup
@app.before_serving
async def startup():
    global assistant, py_tgcalls, clients_initialized
    if not clients_initialized:
        print("Initializing Clients...")
        assistant = Client(
            "assistant_account",
            session_string=os.environ.get("ASSISTANT_SESSION", "")
        )
        await assistant.start()
        py_tgcalls = PyTgCalls(assistant)
        
        # Register pending handlers
        for filter_, handler in pending_update_handlers:
            py_tgcalls.on_update(filter_)(handler)
            
        await py_tgcalls.start()
        clients_initialized = True
        print("Clients Initialized Successfully.")

# Lifecycle: Cleanup on Shutdown
@app.after_serving
async def cleanup():
    global clients_initialized
    if clients_initialized:
        print("Stopping Clients...")
        # Gracefully stop pytgcalls if needed, though usually just stopping the loop is enough
        # await py_tgcalls.stop() 
        await assistant.stop()
        clients_initialized = False

# Download helper: Optimized with concurrency locks but keeping file write logic same
async def download_audio(url: str, api_name: str) -> str:
    if api_name not in DOWNLOAD_APIS:
        raise ValueError(
            f"Unknown API key: {api_name!r}. "
            f"Choose from {list(DOWNLOAD_APIS.keys())}."
        )
    
    # Check cache first
    if url in download_cache:
        # Verify file actually exists
        if os.path.exists(download_cache[url]):
            return download_cache[url]
        else:
            del download_cache[url]

    # Use a lock for this specific URL to prevent race conditions during high concurrency
    if url not in download_locks:
        download_locks[url] = asyncio.Lock()
    
    async with download_locks[url]:
        # Check cache again after acquiring lock (in case another request finished it)
        if url in download_cache and os.path.exists(download_cache[url]):
            return download_cache[url]

        api_base = DOWNLOAD_APIS[api_name]
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3')
        file_name = temp_file.name
        temp_file.close() # Close handle so we can open fresh
        
        download_url = f"{api_base}{url}"
        timeout = aiohttp.ClientTimeout(total=90)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(download_url) as response:
                    response.raise_for_status()
                    # Stream to disk to save RAM (64KB chunks)
                    with open(file_name, 'wb') as f:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            f.write(chunk)

            download_cache[url] = file_name
            return file_name
        except Exception as e:
            if os.path.exists(file_name):
                os.remove(file_name)
            raise Exception(f"Download via '{api_name}' failed: {e}")
        finally:
            # Cleanup lock key to prevent memory leak over long uptime
            if url in download_locks:
                del download_locks[url]

# Play helper
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
async def play():
    chatid = request.args.get('chatid')
    video_url = request.args.get('url')
    api_param = request.args.get('api') or '1'

    # 1) Validate required params
    if not chatid or not video_url:
        return jsonify({'error': 'Missing chatid or url parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    # 2) Map numeric selector -> DOWNLOAD_APIS key
    api_map = {'1': 'default', '2': 'secondary', '3': 'tertiary'}
    if api_param not in api_map:
        return jsonify({
            'error': f"Invalid api '{api_param}'. Choose 1, 2 or 3."
        }), 400
    api_key = api_map[api_param]

    # 3) Start playback (Async)
    try:
        if not clients_initialized:
            # This should be handled by startup(), but redundant check for safety
            return jsonify({'error': 'Server starting up, please wait.'}), 503
            
        await play_media(chat_id, video_url, api_key)
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
async def cache_song():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400

    errors = {}

    # 1) Try default API
    try:
        await download_audio(url, 'default')
        return jsonify({
            'message':    'Song cached successfully',
            'url':        url,
            'api_used':   'default'
        })
    except Exception as e:
        errors['default'] = str(e)

    # 2) Fallback to secondary API
    try:
        await download_audio(url, 'secondary')
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


@app.route('/stop', methods=['GET'])
async def stop():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            return jsonify({'error': 'Clients not initialized'}), 503
            
        await py_tgcalls.leave_call(chat_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Stopped media', 'chatid': chatid})


@app.route('/join', methods=['GET'])
async def join_endpoint():
    chat = request.args.get('chat')
    if not chat:
        return jsonify({'error': 'Missing chat parameter'}), 400

    if re.match(r"https://t\.me/[\w_]+/?", chat):
        chat = chat.split("https://t.me/")[1].strip("/")
    elif chat.startswith("@"):
        chat = chat[1:]

    try:
        if not clients_initialized:
            return jsonify({'error': 'Clients not initialized'}), 503
            
        await assistant.join_chat(chat)
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
async def pause():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            return jsonify({'error': 'Clients not initialized'}), 503
            
        await py_tgcalls.pause(chat_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Paused media', 'chatid': chatid})


@app.route('/resume', methods=['GET'])
async def resume():
    chatid = request.args.get('chatid')
    if not chatid:
        return jsonify({'error': 'Missing chatid parameter'}), 400
    try:
        chat_id = int(chatid)
    except ValueError:
        return jsonify({'error': 'Invalid chatid parameter'}), 400

    try:
        if not clients_initialized:
            return jsonify({'error': 'Clients not initialized'}), 503
            
        await py_tgcalls.resume(chat_id)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'message': 'Resumed media', 'chatid': chatid})

@app.route('/restart', methods=['GET'])
async def restart():
    # Schedule the exit to allow the response to return first
    async def _restart_process():
        await asyncio.sleep(1)
        sys.exit(1)
    
    asyncio.create_task(_restart_process())
    return jsonify({'message': 'Restarting application...'})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    # Hypercorn/Uvicorn is recommended for production, but app.run() works for Quart dev
    app.run(host="0.0.0.0", port=port)
