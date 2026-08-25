# Public Multi-User, Forum, Content-Mode & Operations Update

**Project:** Telegram Media Mirror & Backup Bot  
**Audit version:** `d9eab5f` baseline plus public/forum implementation update  
**Deployment targets:** GitHub `zusty-debug/Backup-mirror` and JustRunMy application `r_r2P6M`

---

## 1. Requested decisions implemented

| Area | Selected behavior |
|---|---|
| Public onboarding | Any Telegram user can open the bot, connect a worker account, and create their own isolated projects. |
| Telegram client API | Shared runtime API ID/API Hash selected for public users. Each user still has a separate Telegram worker session/profile. |
| Forum destination | `CREATE_FORUM` creates a new destination forum group from the authenticated worker account. |
| Default content | Media + files + links. |
| Server transfer | Telegram server-side fresh send; no forwarding API and no normal local download/re-upload for standard transfers. |

---

## 2. Public multi-user support

### Changes made

- Removed the previous owner-only gate from normal bot use.
- Every incoming Telegram user is registered in the `users` table automatically.
- Projects are still owner-isolated by `owner_id`.
- A user can only open, start, pause, stop, report on, or delete their own projects.
- Each user has their own `telegram_profiles` row and encrypted worker-session record.
- The configured `OWNER_IDS` remain administrators rather than the only allowed users.

### Public menu

Every user now receives:

- `🚀 New Backup Project`
- `📂 My Projects`
- `👤 Worker Status`
- `ℹ️ Help`

Administrators additionally receive:

- `🛠️ Admin Panel`

---

## 3. Admin Control Center

The administrator panel now reports global service information:

- total public users;
- total connected worker sessions;
- total projects;
- active/rate-limited projects;
- the administrator's own project counts;
- active worker-task count.

Admin actions include:

- `🔄 Refresh Dashboard`
- `📡 Active Projects` — project name, owner ID, state, source/destination;
- `👥 Worker Sessions` — worker owner ID, masked phone hint, session update time;
- `📂 My Projects`;
- `👤 My Worker`;
- `➕ New Project`.

---

## 4. Worker-account status

The `👤 Worker Status` page shows:

- live worker-session connection check;
- worker display name, username, and Telegram account ID when reachable;
- masked phone hint;
- worker-session added time;
- last session update time;
- current projects in Telegram FloodWait;
- observed `PeerFlood`, `UserRestricted`, or `FloodWait` errors.

Telegram does not provide an authoritative advance spam-block-status endpoint. The bot therefore reports actual observed delivery/restriction errors rather than inventing a status.

---

## 5. Forum/topic cloning

### Create a forum clone

During project creation, send this as the destination:

```text
CREATE_FORUM
```

when the source is a Telegram forum group with topics.

The authenticated worker account creates a new forum supergroup. The project then:

1. identifies source forum topics;
2. creates matching destination topics;
3. stores a durable source-topic → destination-topic mapping in `forum_topics`;
4. sends copied content into the correct destination topic.

### Topic mapping storage

New table:

```text
forum_topics
- project_id
- source_topic_id
- destination_topic_id
- title
- created_at
```

The forum's General topic is mapped directly to the destination General topic. Other topics are created through Telegram's `CreateForumTopic` API.

### Forum limitation deliberately enforced

Custom start from a message ID/link is available for channels and normal groups only. It is rejected for forum-topic copies, which start from the selected forum history so topic mapping and ordering stay correct.

---

## 6. New start options

After creating a project name and content mode, users can choose:

| Start option | Behavior |
|---|---|
| `⏮️ From the beginning` | Processes eligible source content oldest → newest. |
| `🆕 New media only + 300s idle stop` | Begins after the current source tail and watches for new eligible content. |
| `📍 Custom message link / ID` | Accepts a positive source message ID or message link, then begins from that message. Disabled for forums. |

Message links are parsed for their final numeric message ID.

---

## 7. Content modes

A new project now asks what should be copied:

| User option | Result |
|---|---|
| `📄 Files only` | Telegram documents/files such as PDFs, archives, and generic documents. |
| `🎞️ Media only` | Photos, videos, audio, voice notes, video notes, GIFs, stickers, and other media. |
| `🔗 Links only` | Sends new text messages containing discovered URLs, including text-link entities. |
| `📦 Media + Files + Links` | Default mode. |
| `✨ Everything` | Media, files, links, ordinary text, captions, and replies when the parent was also copied. |

`EVERYTHING` copies new content messages. It does not forward source messages or preserve source sender/forward identity.

### Reply preservation

For `EVERYTHING`, the database source-message → destination-message ledger is consulted before sending a copied content item. If a copied parent exists, the new destination message replies to the copied destination parent. Forum topic root mapping is used as the fallback target.

---

## 8. Fast non-forward media transfer

Standard media transfers now use Telegram server-side media reuse:

```text
Source message.media
      ↓
Telegram SendMedia as a new destination message
      ↓
Destination
```

This is not a `ForwardMessages` operation. It does not route the full media file through the hosting container under normal settings.

Download/re-upload remains available only as a fallback path for checksum-enabled projects.

---

## 9. Live progress and 300-second sync timeout

The project status message is refreshed at the configured interval and includes:

- project name and state;
- current phase;
- processed count;
- source messages scanned;
- eligible media/content found;
- copied/skipped/failed totals;
- effective reused-media bytes and effective speed;
- elapsed time;
- current item.

When Continuous Sync finds no eligible new content:

1. it displays `👀 Waiting for new media`;
2. waits up to the per-project idle timer, default **300 seconds**;
3. runs one final scan;
4. transitions to `COMPLETED` if still idle.

If content appears during the final scan, the project continues normally and resets the idle cycle.

---

## 10. Telegram rate-limit behavior

The worker continues to handle Telegram FloodWait responses correctly:

- records the wait state;
- waits for Telegram's required period;
- updates status with the wait phase;
- supports Pause/Stop at safe boundaries;
- resumes without re-copying completed source messages.

---

## 11. UI and attribution

The bot interface now uses emoji labels for project creation, live status, sync, worker account, reports, admin controls, and destructive actions.

Bot replies and live status cards include:

```text
Developed by — @xzusty
```

---

## 12. Database additions

New/used durable data capabilities:

- `forum_topics` mapping table;
- source → destination message lookup for reply preservation;
- per-owner project status summary;
- global admin summary;
- global worker profile list;
- global active-project list;
- observed worker restriction query.

---

## 13. Validation performed

Offline validation after the update:

```text
pytest -q                     9 passed
ruff check app tests          passed
python -m compileall app tests passed
content-mode smoke test       passed
```

Existing test coverage verifies:

- project/transfer database durability;
- restart retry states;
- forum topic mapping persistence;
- settings serialization;
- safe filename handling;
- report generation;
- temporary download behavior;
- server-side media reuse routing without download.

---

## 14. Remaining Telegram behavior constraints

- Telegram service/pin/join/leave messages, polls, reactions, and other non-content events are not recreated as new normal messages.
- Forum cloning recreates topics and routes copied content to mapped topics. Topic title/icon mapping is implemented; forum administrative metadata such as moderator lists, permissions, pinned state, and closed/hidden state are not duplicated by this update.
- `EVERYTHING` preserves reply relationships only when the reply parent is also selected and successfully copied.
- Telegram platform enforcement, including account restrictions or content enforcement, remains Telegram-controlled.

---

## 15. Forum failure visibility and channel-segment alternative

A live forum-clone test surfaced this exact Telegram response:

```text
PremiumAccountRequiredError
caused by CreateForumTopicRequest
```

This means a non-Premium worker account cannot create forum topics through Telegram's client API. The implementation does not attempt to bypass that Telegram restriction.

### New non-Premium forum destination mode

For selected forum topics, users can now send:

```text
CREATE_CHANNEL
```

instead of `CREATE_FORUM`.

The worker creates a normal broadcast destination channel. It processes selected source topics in the order the user selected them:

1. posts a fresh `📌 Topic Name` header;
2. pins that header in the destination channel;
3. mirrors selected topic content below the header;
4. proceeds to the next selected topic and repeats.

The `forum_channel_segments` table stores the destination header message for each source topic so restarts do not create duplicate headers. Direct topic messages are attached under their relevant header; copied replies still map to copied parent messages when available.

### Premium or pre-created-forum mode

- `CREATE_FORUM` remains available for Premium worker accounts and performs topic creation during project setup, before a backup starts.
- For an existing destination forum, the bot maps selected source topics to matching destination topic titles. If a topic is absent, setup stops and reports the exact missing titles before any backup run begins.

Project cards now expose the exact recorded `last_error`, so API failures are visible directly in the bot UI.

## 16. Forum topic selection update

When a user enters a forum source, the bot now lists the accessible topics before destination setup.

The user can:

1. tap one or more topic buttons;
2. use `✅ Select all` if required;
3. tap `➡️ Done`;
4. send `CREATE_FORUM` to create the selected-topic clone.

Only selected source topic IDs are cloned/mapped and processed. The selected IDs are saved in project settings and enforced during the source scan.

## 17. Multi-select content update

Content selection is now multi-select instead of a single fixed choice.

- Files, Media, and Links can be selected in any combination.
- `Everything` is an exclusive all-content choice.
- The selection screen shows `✅` / `⬜` state for each category.
- `➡️ Done` derives the effective content mode automatically.

## 18. Test plan for live Telegram validation

1. Use a second Telegram account to open the public bot and verify it receives the onboarding menu.
2. Connect a distinct worker account and verify it cannot view another user's projects.
3. Create each content-mode project against a small private test chat.
4. Verify Files, Media, Links, multi-select combinations, and Everything separately.
5. Test custom start from a message ID and a message link in a non-forum chat.
6. Create a source forum with at least two named topics; select one topic, use `CREATE_FORUM`, and verify only that topic is cloned/routed.
7. Repeat with `✅ Select all` and verify all selected topics are created and routed.
8. Test a text reply whose parent is also selected in Everything mode.
9. Enable sync, exhaust source content, wait 300 seconds, and confirm final scan then completion.
10. Trigger/rate-limit test only with normal Telegram behavior; confirm FloodWait state and safe resume.

## 19. Product intelligence additions beyond the requested flows

The public version now adds operational capabilities chosen to make the service usable under a shared runtime rather than merely adding more menu buttons.

### Fair public-job scheduler

- Global concurrent backup limit: `MAX_CONCURRENT_BACKUPS` (default `2`).
- Per-user active-job limit: `MAX_ACTIVE_PROJECTS_PER_USER` (default `1`).
- Overflow jobs move to a durable `QUEUED` status instead of starting an unlimited number of Telegram clients at once.
- When a job exits, the scheduler promotes queued jobs fairly while respecting per-user limits.
- Queue events are recorded in project activity.

This protects public users from each other and reduces avoidable Telegram rate-limit pressure.

### Accurate progress and ETA

For one-time/history runs, the worker performs a read-only selected-content count before transfer. Live status can then show:

- exact selected-item total;
- percentage complete;
- item-rate-derived ETA;
- counting-scan progress before transfers begin.

### Read-only Preview Scan

Every project has `🔍 Preview Scan`.

It scans without sending any message and returns:

- source messages inspected;
- total selected items;
- content-type breakdown;
- no-copy confirmation.

This is useful for validating a source, content choices, forum-topic selection, and expected workload before consuming Telegram delivery capacity.

### Operational controls

Every project now has:

- `🧪 Verify Access` — re-checks source readability and destination send access;
- `📜 Activity` — timestamped run, queue, count, rate-limit, error, and completion events;
- `🔁 Retry Failed` — safely resumes retryable failed items;
- `➕ Duplicate Setup` — creates a ready-to-edit copy of an existing project configuration.
