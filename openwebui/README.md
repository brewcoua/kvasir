# Open WebUI functions

Two Pipe functions that drive a [kvasir](../README.md) service. `storm.py` researches a topic and
returns a finished article. `costorm.py` holds a round-table conversation.

They share no code on purpose. Open WebUI stores functions in its database as standalone files and
one cannot import another, so the small duplication between them is deliberate.

## Install

Open WebUI keeps functions in its own database. They are pasted in by hand and are **not**
reconciled from git, so repeat this after changing either file.

1. Open **Admin Panel**, then **Functions**, then **+**.
2. Paste the whole contents of `storm.py`.
3. Save. The name and description come from the header, so leave them.
4. Set `KVASIR_URL` under the function's valves, unless the default
   `http://kvasir:8080` already resolves.
5. Enable the function.
6. Repeat for `costorm.py`.

Verify: **STORM** and **Co-STORM** appear in the model picker. Send a short topic to STORM. Within
a few seconds a status line should report `research: identifying perspectives`. If it never
appears, the service is unreachable, and the chat will say so rather than hang.

Neither file declares a `requirements:` header. Both use only `aiohttp`, `pydantic` and the
standard library, all of which Open WebUI already ships.

## STORM

One model, `STORM`. The last message is the topic, and conversation history is deliberately
ignored: STORM researches a topic from scratch, so a follow-up message starts a new run rather than
continuing one.

A run takes minutes to tens of minutes. Progress appears as status lines, with a heartbeat every 15
seconds carrying elapsed time, because the service goes quiet for minutes during article
generation.

| Valve | Default | Effect |
| --- | --- | --- |
| `KVASIR_URL` | `http://kvasir:8080` | Base URL of the service. |
| `REQUEST_TIMEOUT_SECONDS` | `3600` | Total timeout for one run. |
| `MODEL_FAST` | empty | Overrides the service's fast model. Empty leaves it to the service. |
| `MODEL_STRONG` | empty | Overrides the service's strong model. |
| `SEARCH_TOP_K` | `0` | Search results per query. `0` leaves it to the service. |
| `MAX_CONV_TURN` | `0` | Questions per perspective. `0` leaves it to the service. |
| `MAX_PERSPECTIVE` | `0` | Perspectives to research. `0` leaves it to the service. |
| `POLISH` | `true` | Run the polishing stage. Slower, better prose. |

Empty and zero mean "leave it to the service", so its defaults stay in one place rather than being
shadowed here.

## Co-STORM

One model, `Co-STORM`. One Open WebUI chat is one round table: the session is keyed by the chat id,
so it survives a restart of the service, and a new chat starts a new round table.

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
reply.

| Valve | Default | Effect |
| --- | --- | --- |
| `KVASIR_URL` | `http://kvasir:8080` | Base URL of the service. |
| `REQUEST_TIMEOUT_SECONDS` | `1800` | Total timeout for one call. Warm start is the slow one. |
| `MODEL_FAST` | empty | Overrides the service's fast model. |
| `MODEL_STRONG` | empty | Overrides the service's strong model. |
| `ADVANCE_AFTER_WARM_START` | `true` | Take one agent turn straight after warm start, so the first reply has something to read rather than only a list of experts. |

The embedding model and session lifetime are service configuration, set through
`KVASIR_EMBEDDING_MODEL` and `KVASIR_SESSION_TTL_HOURS`, and are not valves here.

## Common failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Could not reach kvasir at ...` | Wrong `KVASIR_URL`, or the service is down. | Check the URL resolves from the Open WebUI container, and that `/healthz` answers. |
| `kvasir returned 429` | Another run is in progress. | Wait, or raise `KVASIR_MAX_CONCURRENT_RUNS` on the service. |
| An article with no references | SearXNG is not serving JSON. | Add `json` to `search.formats` in its `settings.yml`. |
| `Open WebUI did not supply a chat id` | Co-STORM was called outside a normal chat. | Start a new chat. |
| The chat looks frozen | A slow stage between heartbeats. | Status lines carry elapsed time; a run legitimately takes minutes. |
