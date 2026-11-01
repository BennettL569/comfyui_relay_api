# ComfyUI Relay API Nodes + Relay Manager

[中文](README.md)

Use text, image and video models through third-party API relays in Comfy Desktop and ComfyUI, with as many relays as you like. A built-in manager lets you paste an address and token to add a relay, test it, browse its models and drop a configured node onto the canvas.

It works with OpenAI-compatible relays such as those built on new-api or one-api, and also supports the Gemini native API, the Sora format and vendor-specific formats.

![Relay manager and nodes](docs/screenshot.png)

> This is only a client. It provides no API or credit. Tokens and charges come from your own relay, and image and video generation will really cost money.

The interface is in English. When ComfyUI's language is set to Chinese, the nodes and sidebar panel switch to Chinese (as in the screenshot), and the standalone window has a language button. Pull requests for other languages are welcome.

## Install

Made mainly for Comfy Desktop, and it works with other ComfyUI installs too. You need a local instance (tested on ComfyUI 0.43.2; update first if the nodes fail to load). There are no extra dependencies.

### Comfy Desktop

Desktop's built-in extension manager only searches the Comfy Registry, where this plugin isn't listed yet, so install it by hand for now:

1. On GitHub, choose Code → Download ZIP.
2. In Desktop's instance menu, click Open Folder and go to `ComfyUI\custom_nodes` inside it.
3. Extract the ZIP there. `__init__.py` must sit directly in the plugin folder (`custom_nodes\comfyui_relay_api-main\__init__.py`), without an extra folder level.
4. Restart the instance. A new link icon labelled Relay appears in the left sidebar.

Use a local instance: Comfy Cloud runs remotely and can't load this plugin.

### Other ComfyUI installs

From the command line:

```bash
cd <ComfyUI folder>/custom_nodes
git clone https://github.com/BennettL569/comfyui_relay_api.git
```

You can also use the ZIP above, or Install via Git URL in the legacy ComfyUI Manager interface. Restart ComfyUI afterwards.

### Update and uninstall

**Update:** extract a new ZIP over the old folder, or run `git pull` in the plugin folder if you installed with git. Then restart the instance. Your relays are kept. Desktop's own ComfyUI updates don't touch this plugin.

**Uninstall:** delete the plugin folder. It also holds your `relays.json`, so back that up first if you need it.

## Quick start

1. Open the Relay Manager window. Click the link icon in the left sidebar: the panel shows the path of the start file. Click Copy path, paste it into the File Explorer address bar or the Run box (Win+R), and press Enter. You can also double-click `tools\start_manager.bat` in the plugin folder, and `tools\create_shortcut.bat` adds desktop and Start menu shortcuts. On macOS and Linux, run `sh tools/start_manager.sh`.
2. Click Add relay and paste the address and token your relay gave you into the top box. They are detected and filled in for you.
3. Click Test connection, then Save.
4. Find a model in the list and click Add to canvas. A configured node appears in the open ComfyUI. Write a prompt in it and run it.

## Relay Manager

There are two ways in, sharing one interface and one config:

- **The window:** add, change and test relays here, with or without ComfyUI running. If ComfyUI is running, changes sync to it and Add to canvas places the node there.
- **The ComfyUI sidebar panel:** view relays and models, test saved relays and add nodes to the canvas. By default relays can't be changed here (see Security). To allow it, tick "Allow changing relays in the ComfyUI sidebar" in the window.

## Nodes

Search for `relay` in the node library:

| Node | What it does |
|---|---|
| Relay Chat | Calls `/v1/chat/completions`. Connect an image for vision models |
| Relay Image | Text-to-image and image editing, with multiple reference images |
| Relay Video | Text-to-video and image-to-video. Polls progress and downloads the result |
| Relay Models | Lists the relay's models |

**Image formats:** `images`, `images-json`, `chat`, `gemini`.

**Video formats:**

| Format | Request | Typical use |
|---|---|---|
| unified | `POST /v1/video/generations` | new-api's unified video endpoint |
| openai | `POST /v1/videos` | Sora format |
| chat | `/v1/chat/completions`, with a video link in the reply | Some relays' veo and sora |
| custom | Your own submit and query paths | Vendor-specific formats |
| task | `POST /v1/videos`, then `GET /v1/tasks/{task_id}` and artifacts | new-api task-plugin models (`owned_by: task plugin`). Leave the plugin field empty so the relay picks it |

The manager recommends a format for each model. If it fails, try another one: errors include the relay's own message.

Once a video task is submitted it may already be charged, so failed status checks are retried and the task is never resubmitted. If it still can't be checked, the error includes the task ID.

## Configuration

The manager edits `relays.json` in the plugin folder, created the first time you save. You can also edit it by hand (see `relays.example.json`) and press R in ComfyUI to refresh:

```json
{
  "my-relay": {"base_url": "https://api.example.com/v1", "api_key": "sk-...", "proxy": ""}
}
```

Use `"api_key_env": "ENV_VAR_NAME"` instead of `api_key` to read the token from an environment variable, or set `COMFY_RELAY_CONFIG` to keep the file elsewhere.

## Security

- Tokens stay in `relays.json`. They are not written to workflows or image metadata, and the manager only shows them masked. Don't share this file. It and its backup are in `.gitignore`.
- Tokens are only sent to the relay's own origin when downloading results.
- The standalone manager listens on 127.0.0.1 only and uses a one-time token per launch.
- Workflows only choose a relay by name. They can't change its address or token, or read the token.
- ComfyUI's routes can't add or change relays or test unsaved addresses by default: ComfyUI has no login, and a request through a tunnel looks local. Only the window, with its one-time token, can. Ticking "Allow changing relays in the ComfyUI sidebar" also allows same-origin requests from the computer running ComfyUI. That setting lives in the config file and ComfyUI's routes can't change it. If ComfyUI runs on a remote machine, edit `relays.json` there.
- If you change a relay's address or proxy, you must enter its token again, so a stored token is never sent to a new host.
- Download addresses in API replies that aren't on the relay's own origin can't point to loopback or private networks, so a workflow can't use a model's reply to read internal pages, and errors don't include what such hosts return. If your files really are on a private network, set `COMFY_RELAY_ALLOW_PRIVATE_DOWNLOADS=1` on that computer.
- Requests carrying your token don't follow redirects to other sites.
- ComfyUI itself has no login. If you expose it with `--listen`, anyone who can reach it can run workflows through your relays. They can't see your tokens, but the charges are yours.

## Tests

Use the Python that runs ComfyUI (`aiohttp av numpy pillow` are enough for the first two):

```bash
python -X utf8 tests/test_client.py
python -X utf8 tests/test_manager.py
python -X utf8 tests/test_e2e.py <ComfyUI folder>
python -X utf8 tests/check_relays.py   # free, read-only check of your configured relays
```

The nodes and manager have been verified on Comfy Desktop on Windows, including text-to-video against a real relay. On macOS and Linux only the first two tests run automatically, and the nodes themselves are untested. Please open an issue if you hit a problem.

## License

[MIT](LICENSE)
