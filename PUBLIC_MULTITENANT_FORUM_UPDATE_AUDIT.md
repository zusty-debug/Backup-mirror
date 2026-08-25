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

## 15. Test plan for live Telegram validation

1. Use a second Telegram account to open the public bot and verify it receives the onboarding menu.
2. Connect a distinct worker account and verify it cannot view another user's projects.
3. Create each content-mode project against a small private test chat.
4. Verify Files, Media, Links, and Everything modes separately.
5. Test custom start from a message ID and a message link in a non-forum chat.
6. Create a source forum with at least two named topics; use `CREATE_FORUM`; verify destination forum/topics and topic-routing.
7. Test a text reply whose parent is also selected in Everything mode.
8. Enable sync, exhaust source content, wait 300 seconds, and confirm final scan then completion.
9. Trigger/rate-limit test only with normal Telegram behavior; confirm FloodWait state and safe resume.
