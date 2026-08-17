# Open WebUI function

One Pipe function, `pipe.py`, that drives a [kvasir](../README.md) service and exposes two models:
**STORM** researches a topic and returns a cited article, **Co-STORM** holds a round-table
conversation.

## Install

Open WebUI keeps functions in its own database. It reads this file once, at import, and does
**not** reconcile it from git afterwards, so repeat this after changing the file.

1. Open **Admin Panel**, then **Functions**.
2. Open the menu beside **+** and choose **Import From Link**.
3. Paste the raw URL and import:

   ```
   https://github.com/brewcoua/kvasir/releases/latest/download/pipe.py
   ```

   Importing runs the file's code on your server, so read it first. That URL follows whatever the
   newest release is; swap `latest/download` for `download/v0.1.0` to pin one.

   Each release attests the file it publishes, so you can check it came from this repository's
   build rather than trusting the URL:

   ```
   gh release download --pattern pipe.py --repo brewcoua/kvasir
   gh attestation verify pipe.py --repo brewcoua/kvasir
   ```
4. On the create page, set the id to `kvasir`. The id prefixes both models, so this is what makes
   them `kvasir.storm` and `kvasir.co-storm`. The name and description come from the file's
   header, so leave them.
5. Save, then set `KVASIR_URL` under the function's valves, unless the default
   `http://kvasir:8080` already resolves.
6. Enable the function.

Verify: **STORM** and **Co-STORM** appear in the model picker. Send a short topic to STORM. Within
a few seconds the status line should read `research: identifying perspectives` and a collapsible
block below it should start filling with stage lines. If neither appears, the service is
unreachable, and the chat will say so rather than hang.

The file declares no `requirements:` header. It uses only `aiohttp`, `pydantic` and the standard
library, all of which Open WebUI already ships.

## What a run looks like

A run takes minutes to tens of minutes, so the function reports as it goes rather than returning
one silent block at the end.

- **Progress lands in the message.** Every stage change and every detail the service reports is
  written into a reasoning block. Open WebUI renders it collapsed while streaming and times it
  itself, so the finished message reads `Thought for 8m 41s` with the whole log inside. Expand it
  to see which perspectives were identified, which sections were written, and when.
- **Progress also lands in the status line**, with a heartbeat every 15 seconds carrying elapsed
  time, because the service goes quiet for minutes during article generation.
- **Sources become citations.** Each source is emitted as a citation, so Open WebUI renders the
  reference chips and the sources panel instead of a markdown list at the bottom. The citation
  name carries the number, `[3] Parish survey`, because the article's `[n]` markers are what map a
  passage back to a source.
- **The run reports what it spent.** A collapsed footer gives the run id, per-stage timings, tokens
  and cost, split by role. A gateway that prices nothing shows `cost not reported`, which is not
  the same as free. Turn it off with `SHOW_USAGE`.
- **The chat names itself** after the topic, on the first message only.
- **A toast fires** on completion and on failure.

## STORM

The last message is the topic, and conversation history is deliberately ignored: STORM researches
a topic from scratch, so a follow-up message starts a new run rather than continuing one. Because
that is expensive to do by accident, a follow-up in a chat that already holds an article asks for
confirmation first. Turn that off with `CONFIRM_RERUN`.

Sending an empty message opens an input dialog asking for a topic.

## Co-STORM

One Open WebUI chat is one round table: the session is keyed by the chat id, so it survives a
restart of the service, and a new chat starts a new round table.

The first message is the topic and runs warm start, which takes minutes. Afterwards:

| Message | Effect |
| --- | --- |
| `next` | Advance the round table without speaking. |
| `report` | Generate the report so far. |
| anything else | Said to the round table, and answered. |

Matching ignores case and strips one leading `/`, so `/next` and `next` both work.

`/next` needs a little setup, and `next` needs none. Open WebUI refuses to send a message whose
leading `/token` is not a registered command, and registering one is a **Prompt** under
**Workspace**, not part of this function. To use the slash form, create a Prompt with command
`next` whose content is `/next`, and another for `report`. Both are optional.

Saying something costs two calls to the service: upstream records your utterance and returns
without answering it, so the function immediately asks for a response as well. Both appear as one
reply, under one reasoning block.

## Valves

| Valve | Default | Effect |
| --- | --- | --- |
| `KVASIR_URL` | `http://kvasir:8080` | Base URL of the service. |
| `RESEARCH_TIMEOUT_SECONDS` | `3600` | Total timeout for one STORM run. |
| `SESSION_TIMEOUT_SECONDS` | `1800` | Total timeout for one Co-STORM call. Warm start is the slow one. |
| `MODEL_FAST` | empty | Overrides the service's fast model. Empty leaves it to the service. |
| `MODEL_STRONG` | empty | Overrides the service's strong model. |
| `SEARCH_TOP_K` | `0` | Search results per query. `0` leaves it to the service. |
| `MAX_CONV_TURN` | `0` | Questions per perspective. `0` leaves it to the service. |
| `MAX_PERSPECTIVE` | `0` | Perspectives to research. `0` leaves it to the service. |
| `POLISH` | `true` | Run the polishing stage. Slower, better prose. |
| `ADVANCE_AFTER_WARM_START` | `true` | Take one agent turn straight after warm start, so the first reply has something to read rather than only a list of experts. |
| `CONFIRM_RERUN` | `true` | Ask before researching again in a chat that already holds an article. |
| `SHOW_USAGE` | `true` | Append what the run spent. |
| `SET_CHAT_TITLE` | `true` | Name the chat after the topic. |

Empty and zero mean "leave it to the service", so its defaults stay in one place rather than being
shadowed here.

Users can override five of these for themselves, under the function's user valves:
`SEARCH_TOP_K`, `MAX_CONV_TURN` and `MAX_PERSPECTIVE`, where `0` defers to the admin valve, and
`POLISH` and `SHOW_USAGE`, where `default` defers and `on` or `off` decides.

The embedding model and session lifetime are service configuration, set through
`KVASIR_EMBEDDING_MODEL` and `KVASIR_SESSION_TTL_HOURS`, and are not valves here.

## Common failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Could not reach kvasir at ...` | Wrong `KVASIR_URL`, or the service is down. | Check the URL resolves from the Open WebUI container, and that `/healthz` answers. |
| `kvasir returned 429` | Another run is in progress. | Wait, or raise `KVASIR_MAX_CONCURRENT_RUNS` on the service. |
| An article with no references | SearXNG is not serving JSON. | Add `json` to `search.formats` in its `settings.yml`. |
| `Open WebUI did not supply a chat id` | Co-STORM was called outside a normal chat. | Start a new chat. |
| Progress appears as plain text rather than a collapsible block | Reasoning tag detection is off for this model. | Set **Reasoning Tags** to **Default** or **Enabled** under the model's advanced parameters. |
| A confirmation dialog never appears | The browser tab is closed, or `WEBSOCKET_EVENT_CALLER_TIMEOUT` elapsed. | Neither blocks the run: it proceeds as if confirmed. |
