# 条件请求对象源站 API

基于 **Python + FastAPI + PostgreSQL** 的对象源站，完整实现 RFC 9110 条件请求与字节范围服务：

- 客户端按路径 `PUT /objects/{path}` 发布内容修订；每次发布生成新修订。
- 以内容的 **SHA-256** 生成**强 ETag**（`"<64 hex>"`），`Last-Modified` 为修订创建时间（秒级精度）。
- 所有方法按 **RFC 9110 §13.2.2** 的顺序求值前置条件：
  `If-Match` → `If-Unmodified-Since`（仅当无 `If-Match`）→ `If-None-Match` → `If-Modified-Since`（仅 GET/HEAD 且无 `If-None-Match`）。
  条件失败返回 **412**，GET/HEAD 命中 `If-None-Match` / `If-Modified-Since` 返回 **304**。
- 条件求值与修订选择发生在**同一事务**内：读请求对对象行加 `FOR SHARE` 锁、写请求加 `FOR UPDATE` 锁，并发发布串行化，杜绝并发覆盖（`If-None-Match: *` 的"仅创建"语义在并发下也只有一个成功）。
- GET/HEAD **先处理前置条件，再处理 Range**：支持单段（`bytes=0-99`、`bytes=0-`）、后缀（`bytes=-500`）与多段范围；重叠或相邻区间按升序合并；分别返回 200 / 206 / 304 / 416。
- 多段范围返回 `multipart/byteranges`，**边界由修订 ETag + 区间集合派生**，重复请求产生完全相同的边界与 `Content-Range`。
- `If-Range`（ETag 强比较或 HTTP 日期）失配时忽略 Range，返回完整实体（200）。
- 每次响应绑定同一修订：流式传输期间事务与行锁保持打开，此时发布新版本**不会混入**内容或元数据。
- **可恢复复制会话**：`POST /copy-sessions` 在同一数据库快照中锁定源路径集合与全部目标路径，生成按稳定路径与修订序排列、带 SHA-256 内容摘要的清单，并为每个目标路径（含目标尚不存在的状态）保存**修订身份基线**；客户端按游标逐段拉取内容（重复拉取字节、摘要、游标完全一致，即使源对象随后发布新修订），逐段确认时服务锁定目标并与基线比较修订身份——**建档后目标路径的任何发布（哪怕字节与待复制内容完全相同）都判为冲突**，返回 `409`、冲突对象与恢复位置，整批不应用、不推进游标；基线未变时维持原子应用与会话前同内容目标的幂等跳过，重复确认或重放返回同一批次结果。
- **历史修订回收**：`POST /gc` 提交 1～100 个对象路径（服务去重）、UTC 截止时间 `before` 与布尔 `dry_run`；只考虑创建时间**严格早于** `before` 的修订，且**当前修订、复制会话清单或目标基线引用的修订、有效历史修订租约引用的修订一律保留**。`dry_run`（缺省 `true`）在可重复读快照中按路径排序返回候选修订号、可释放字节数与被保留修订的原因（含 `revision_lease`），不改数据；`dry_run=false` 按固定路径升序锁定全部对象行，在**同一事务内重新判定并删除**，整批成功或回滚——与 PUT、复制会话建档/确认、租约创建、流式 GET 并发时不会误删在用修订或留下悬空引用；重复执行返回空清单，后续发布的修订号仍递增。**已迁走的路径**（`POST /moves`）没有当前修订但仍保留修订历史，同样可以被回收：未被任何会话或租约引用的旧修订正常清理，而会话清单/基线/有效租约钉住的修订一律保留。
- **历史修订租约**：`POST /revision-leases` 提交 `path`、**正整数** `revision_no` 与 `ttl_seconds`（**60～86400 秒**），只能为**仍在库中**的修订（含当前修订、含已迁走路径下残留的历史修订）创建：服务先对对象行加 `FOR UPDATE` 行锁再查修订——与 `/gc` 执行共用同一全局锁序，因此**创建与回收按对象行严格串行**，要么租约先生效（`/gc` 的锁内重判定看到它、保留该修订），要么回收先提交（租约请求得到 404），绝不会"租约成功却丢了修订"。成功返回 **201**、随机租约 ID（32 位十六进制）、数据库时钟计算的 `created_at`/`expires_at` 以及绑定修订的强 ETag（响应头同时带 `ETag`/`Last-Modified`）；修订不存在返回 **404**（`revision_not_found`），参数非法返回 **400**。`GET/HEAD /revision-leases/{id}/content` 按**绑定的旧修订**执行与 `/objects` 完全相同的前置条件（304/412）与 Range/If-Range 规则（200/206/416）；绑定在创建时确定，**路径重新发布、迁移或重放请求都不改变响应**。读取先查租约再对对象行加 `FOR SHARE`、随后复查租约状态与修订是否存在，事务一直持有到最后一个字节——**读取开始后即使租约到期或被释放，流仍完整、元数据始终属于绑定修订**，`/gc` 会等待流结束才能删除。`DELETE /revision-leases/{id}` **幂等释放**：活动、已释放、已过期的租约都返回 204，从未存在的 ID 返回 **404**；释放/过期后再读内容返回 **410**（`lease_released` / `lease_expired`，含路径、修订号与时间戳），修订本身随后才可能被下一次 `/gc` 回收。
- **对象路径迁移**：`POST /moves` 提交非空且互不相同的 `source_path`、`target_path`、`source_etag`（强 ETag）与正整数 `source_revision_no`；仅当**源强 ETag 与修订号都匹配当前修订、且目标当前无内容**时，按路径升序同时锁住两端，在**一个事务**内把源内容与媒体类型发布为目标的**新修订**（目标侧按自身历史 `max+1` 编号），并把源路径摘除（其 GET/HEAD 随即 **404**）。源不存在（含已迁走）返回 **404**，源已变（ETag 或修订号不符）返回 **412**（响应带当前 ETag/修订号），目标已有内容返回 **409**；任何失败整事务回滚，不留部分生效。已迁走的路径再次 PUT 或作为迁移目标复用时，修订号由每路径高水位线 `revision_seq` 锚定，**只增不复用、不回退**——即便旧修订已被 `/gc` 全部清理。
- **对象合成**：`POST /compositions` 提交目标路径、目标基线（不存在为 `null`，否则给出修订号与强 ETag）、目标媒体类型与**有序的 1～100 个源片段**；每个片段指定源路径、修订号、强 ETag 与**左闭右开**字节区间 `[start, end)`，同一源修订可重复取段。服务在**单一事务**里先用一条按路径升序的纯 `FOR UPDATE` 锁住全部源行与目标行（只为目标补占位行；这就是**源修订钉住与目标锁定的顺序裁决**：不做"源 SHARE/目标 UPDATE"两段加锁，否则两组交叉锁集会死锁），锁内才按 `(路径, 修订号)` 解析**不可变修订行**、核验每段强 ETag、目标基线与区间边界，再按请求顺序拼接原始字节、发布目标新修订并返回修订号、强 ETag 与总字节数（201）。空段或越界返回 **400**，源修订不在库返回 **404**（段索引、路径、修订号），任一源 ETag 或目标基线不符返回 **412**（404 → 412 → 400 的固定裁决顺序）；**任何失败整事务回滚，不留目标修订、不留占位行**。并发 PUT（源或目标）、`POST /moves` 迁移、`POST /gc` 回收都按同一全局路径锁序串行：既不会把发布后的新字节混进合成结果，`/gc` 也删不掉事务仍在读取的源修订，交叉锁集的并发合成**不会死锁**；目标与源是同一路径时仍从指定的旧修订取段（而不是正在发布的新修订），条件读写、复制会话、迁移与回收的既有语义完全不变。
- **对象索引（十分钟快照分页）**：`GET /object-index?prefix=&limit=&cursor=` 按路径升序列举当前对象，每条固化 `path`、`revision_no`、`ETag`、`media_type`、`bytes` 五项元数据。`prefix` 可省略（空串表示**全量**），`limit` 缺省 **50**、强制 **1～100**；首次请求由索引模块在**无行锁**的可重复读快照中遍历对象并把结果物化为一份**十分钟快照**（建快照不阻塞任何 PUT、迁移、复制或回收），随后用返回的 `next_cursor` 翻页时先校验 HMAC 签名与其中的 `prefix`、`limit`、快照 ID，再**只回放该快照副本**——翻页期间发生 PUT、路径迁移或历史回收都**不改变、不重复、不遗漏**任何条目；游标伪造、参数非法或与游标不符返回 **400**，快照过期（或已清理）返回 **410**（`snapshot_expired`），重新发起无游标请求即建立新快照。每次请求顺带清理过期快照：清理是页事务**之外**独立的读已提交 DELETE，多个请求同时清理同一份过期快照时在行锁上串行（输家删除 0 行），不会触发可重复读 40001 而返回 500；而存活判定与取页仍在同一个可重复读事务内，单页的过期裁决与内容始终一致。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>。PostgreSQL 数据保存在 `pgdata` 卷中；表结构由应用启动时自动创建。

健康检查：

```bash
curl http://localhost:8000/healthz
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `PUT` | `/objects/{path}` | 发布新修订；请求体为内容，`Content-Type` 被保存；响应 `204` + `ETag` / `Last-Modified` / `X-Revision-No` |
| `GET` | `/objects/{path}` | 读取当前修订；支持全部条件头与 `Range` / `If-Range` |
| `HEAD` | `/objects/{path}` | 同 GET，仅返回头部 |
| `POST` | `/moves` | 原子路径迁移：把源当前修订（按强 ETag + 修订号锁定）发布为目标新修订并摘除源；201 / 404 / 412 / 409，失败整事务回滚 |
| `POST` | `/compositions` | 对象合成：1～100 个源片段（路径 + 修订号 + 强 ETag + 左闭右开区间）按序拼接发布为目标新修订，可带目标基线；201 / 400 / 404 / 412，失败不留目标修订 |
| `POST` | `/copy-sessions` | 创建复制会话：提交 `source_paths`、`target_prefix`、可选 `cursor`（上次确认游标）与 `segment_size`；响应 `201` + 带 SHA-256 摘要的清单 |
| `GET` | `/copy-sessions/{id}` | 会话状态：清单摘要、分段进度、恢复位置 |
| `GET` | `/copy-sessions/{id}/segments?cursor=` | 拉取游标之后的内容分段（无 `cursor` 参数时返回首段）；重复拉取字节、摘要、游标完全一致 |
| `POST` | `/copy-sessions/{id}/acks` | 按游标确认分段：锁定目标并与建档修订基线比较身份，原子批次应用；建档后任何发布（含同字节重发）返回 `409`，整批不应用、不推进游标；重复确认 / 重放返回同一批次结果 |
| `POST` | `/gc` | 历史修订回收：提交 `paths`（1～100，去重）、UTC 截止时间 `before` 与 `dry_run`；预览与执行都返回候选/已删修订及保留原因（含 `revision_lease`），执行在同一事务内重判定并删除（整批成功或回滚）；路径不存在整次 `404` |
| `POST` | `/revision-leases` | 为仍在库中的（路径, 正整数修订号）创建租约，TTL 60～86400 秒；201 返回随机租约 ID、到期时间与绑定修订 ETag；修订不存在 404，参数非法 400 |
| `GET` / `HEAD` | `/revision-leases/{id}/content` | 按绑定的旧修订执行条件请求与 Range 规则（200/206/304/412/416）；未知 ID 404，租约过期或已释放 410；读取开始后到期/释放不影响进行中的流 |
| `DELETE` | `/revision-leases/{id}` | 幂等释放租约（活动/已释放/已过期均 204）；未知 ID 404 |
| `GET` | `/object-index` | 按路径升序分页列举对象（`prefix` 可空=全量、`limit` 缺省 50/范围 1～100、`cursor` 翻页）；首次建立十分钟快照，之后回放同一快照；非法参数/篡改游标 400，快照过期 410 |
| `GET` | `/healthz` | 健康检查 |

支持的条件头：`If-Match`、`If-None-Match`、`If-Modified-Since`、`If-Unmodified-Since`（PUT/GET/HEAD 均按 §13.2.2 顺序求值）。

## curl 验证

```bash
# 1. 发布内容（记下返回的 ETag 与 Last-Modified）
curl -i -X PUT --data-binary 'hello world' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/demo.txt
# HTTP/1.1 204 ...  ETag: "b94d27b9..."  Last-Modified: Tue, 22 Sep 2026 ...

ETAG=$(curl -sI http://localhost:8000/objects/demo.txt | awk '/^[Ee][Tt]ag:/ {print $2}' | tr -d '\r')

# 2. 条件 GET：ETag 未变 -> 304（If-None-Match 用弱比较，W/"..." 形式同样命中）
curl -i -H "If-None-Match: $ETAG" http://localhost:8000/objects/demo.txt
curl -i -H "If-None-Match: W/${ETAG//\"/\\\"}" http://localhost:8000/objects/demo.txt

# 3. If-Modified-Since -> 304；If-Match 不匹配 -> 412（强比较，W/ 形式必失败）
curl -i -H "If-Modified-Since: $(curl -sI http://localhost:8000/objects/demo.txt | awk '/^[Ll]ast-[Mm]odified:/ {sub(/^[^:]*: /,""); print}' | tr -d '\r')" \
  http://localhost:8000/objects/demo.txt
curl -i -H 'If-Match: "stale"' http://localhost:8000/objects/demo.txt

# 4. 乐观并发写：仅当未修改才覆盖
curl -i -X PUT --data-binary 'v2' -H "If-Match: $ETAG" http://localhost:8000/objects/demo.txt   # 204
curl -i -X PUT --data-binary 'v3' -H "If-Match: $ETAG" http://localhost:8000/objects/demo.txt   # 412（ETag 已变）

# 5. 仅创建语义：已存在则 412
curl -i -X PUT --data-binary 'x' -H 'If-None-Match: *' http://localhost:8000/objects/demo.txt   # 412

# 6. 字节范围
curl -i -H 'Range: bytes=0-4'   http://localhost:8000/objects/demo.txt   # 206 + Content-Range
curl -i -H 'Range: bytes=-5'    http://localhost:8000/objects/demo.txt   # 后缀范围
curl -i -H 'Range: bytes=0-99999' http://localhost:8000/objects/demo.txt # 截断到末尾
curl -i -H 'Range: bytes=99999-' http://localhost:8000/objects/demo.txt  # 416 + Content-Range: bytes */N

# 7. 多段范围：重叠/相邻自动合并；重复请求边界与 Content-Range 完全一致
curl -i -H 'Range: bytes=0-1,2-3' http://localhost:8000/objects/demo.txt # 合并为单段 206
curl -i -H 'Range: bytes=0-1,5-9' http://localhost:8000/objects/demo.txt # multipart/byteranges
curl -s -H 'Range: bytes=0-1,5-9' http://localhost:8000/objects/demo.txt | sha256sum
curl -s -H 'Range: bytes=0-1,5-9' http://localhost:8000/objects/demo.txt | sha256sum   # 两次相同

# 8. If-Range：失配返回完整实体（200），匹配才返回 206
curl -i -H 'Range: bytes=0-4' -H 'If-Range: "stale"' http://localhost:8000/objects/demo.txt  # 200
curl -i -H 'Range: bytes=0-4' -H "If-Range: $ETAG"   http://localhost:8000/objects/demo.txt  # 206
```

## 对象路径迁移（POST /moves）

把一个对象按路径整体迁移到一个**当前为空**的新路径。请求同时给出源当前修订的**强 ETag 与修订号**作为乐观锁：

```bash
# 准备：源对象发布到修订 2
curl -si -X PUT --data-binary 'quarterly results' \
  -H 'Content-Type: text/plain; charset=utf-8' \
  http://localhost:8000/objects/demo/report.txt | grep -i x-revision-no   # X-Revision-No: 1
curl -si -X PUT --data-binary 'quarterly results v2' \
  -H 'Content-Type: text/plain; charset=utf-8' \
  http://localhost:8000/objects/demo/report.txt | grep -iE 'etag|x-revision-no'
# ETag: "a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8"
# X-Revision-No: 2

# 迁移：源 ETag + 修订号都匹配、目标为空时才生效（单事务）
curl -i -X POST http://localhost:8000/moves \
  -H 'Content-Type: application/json' \
  -d '{"source_path": "demo/report.txt",
       "target_path": "archive/2026/report.txt",
       "source_etag": "\"a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8\"",
       "source_revision_no": 2}'
# HTTP/1.1 201 Created
# ETag: "a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8"
# Last-Modified: Wed, 30 Sep 2026 01:33:52 GMT
# X-Revision-No: 1
# Content-Type: application/json
#
# {
#   "source_path": "demo/report.txt",
#   "target_path": "archive/2026/report.txt",
#   "target_etag": "\"a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8\"",
#   "target_revision_no": 1,
#   "source_etag": "\"a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8\"",
#   "source_revision_no": 2
# }

# 源路径已摘除：GET/HEAD 都是 404；目标以自身的修订 1 提供相同内容与媒体类型
curl -i  http://localhost:8000/objects/demo/report.txt            # 404 {"detail":"Not Found"}
curl -sI http://localhost:8000/objects/demo/report.txt | head -1  # HTTP/1.1 404 Not Found
curl -i  http://localhost:8000/objects/archive/2026/report.txt   # 200，ETag 与源一致，text/plain

# 在已迁走的旧路径上重新 PUT：修订号接续（3），绝不回退或复用 1/2
curl -si -X PUT --data-binary 'new file at old path' \
  http://localhost:8000/objects/demo/report.txt | grep -i x-revision-no          # X-Revision-No: 3

# 失败一：源不存在（路径从未发布，或已经迁走）-> 404
curl -i -X POST http://localhost:8000/moves -H 'Content-Type: application/json' \
  -d '{"source_path":"demo/report.txt","target_path":"x/y.txt",
       "source_etag":"\"a7fc813eb2f62294d9fc1408c4741fe3a4c0c75f75494d80297f2bfcd5e517b8\"",
       "source_revision_no":2}'
# HTTP/1.1 404 Not Found
# {"detail":"source_not_found","source_path":"demo/report.txt"}

# 失败二：源已变（ETag 与修订号均不符）-> 412，响应给出当前 ETag/修订号
curl -i -X POST http://localhost:8000/moves -H 'Content-Type: application/json' \
  -d '{"source_path":"s.txt","target_path":"t.txt",
       "source_etag":"\"stale\"","source_revision_no":1}'
# HTTP/1.1 412 Precondition Failed
# ETag: "f9ef50d69e756978acc0ac3dcbc444ae28cfae32cd1449d4dfcd8849d8a02b31"
# {"detail":"precondition_failed","source_path":"s.txt",
#  "mismatches":["source_etag","source_revision_no"],
#  "current_etag":"\"f9ef50d69e756978acc0ac3dcbc444ae28cfae32cd1449d4dfcd8849d8a02b31\"",
#  "current_revision_no":2}

# 失败三：目标已有内容 -> 409（整事务回滚，源保持不动、目标保持原样）
curl -i -X POST http://localhost:8000/moves -H 'Content-Type: application/json' \
  -d '{"source_path":"s.txt","target_path":"busy.txt",
       "source_etag":"\"f9ef50d69e756978acc0ac3dcbc444ae28cfae32cd1449d4dfcd8849d8a02b31\"",
       "source_revision_no":2}'
# HTTP/1.1 409 Conflict
# {"detail":"target_already_exists","target_path":"busy.txt",
#  "current_etag":"\"20323a24be105b1b519962994b8e4e6a7f8e3cd0d005b8ee10c9aeb66da5d40a\"",
#  "current_revision_no":1}

# 请求体校验：四个字段必填；source_etag 必须是强 ETag（W/ 弱形式 400）；
# source_revision_no 必须是正整数；两端必须非空且不同（400）。
```

迁移与既有机制的关系：

- **复制会话不受迁移影响**：迁移前创建的会话清单钉住的是**不可变修订行**（源路径的旧修订），迁移只摘除源的 `current_id` 指针、不删修订，所以会话仍可逐段拉取已固定的源修订；会话目标基线在迁移后若发生变化（例如有人把另一个对象迁到了该目标），确认时仍按原规则报 **409**、整批不应用。要复制迁移后的新位置，用新路径重新建档即可。
- **历史回收覆盖已迁走路径**：迁走后源路径没有当前修订，其旧修订不再被 `current` 原因保留；`/gc` 可以清理其中**未被任何会话清单/基线引用**的修订，会话固定的修订仍被保留、不会误删。
- **并发安全**：迁移与 PUT、复制会话建档/确认、`/gc`、流式 GET 共用同一套"先按路径升序锁 `objects` 行、锁内再读修订"的规则；两个迁移交换/重叠端点时按相同加锁顺序串行化，不会死锁，也不会出现部分生效或修订号重复。


## 对象合成（POST /compositions）

把多个源对象的**指定旧修订**按**左闭右开字节区间**切成片段，严格按客户端给的顺序拼接成字节流，在**单一事务**中发布为目标路径的新修订。

请求体：

| 字段 | 规则 |
| --- | --- |
| `target_path` | 必填，非空字符串 |
| `target_baseline` | 必填：目标不存在时为 `null`；否则为 `{"revision_no": <正整数>, "etag": <强 ETag>}` |
| `media_type` | 必填，非空字符串，作为新修订的媒体类型（与源媒体类型无关） |
| `segments` | 必填，**1～100** 个片段的**有序数组**，按数组顺序拼接 |
| `segments[].path` / `revision_no` / `etag` | 源路径、正整数修订号、**强** ETag（`W/` 弱形式 400） |
| `segments[].start` / `end` | 非负整数，**左闭右开**区间 `[start, end)`；`start == end` 是空段（400），`end > 源修订字节数` 越界（400），`end == 字节数` 合法 |

同一源修订可以在数组里重复出现、区间可以重叠，顺序和区间完全由客户端决定。状态码按 **404（源修订不在库）→ 412（源 ETag / 目标基线不符）→ 400（空段/越界）** 的顺序裁决；请求体结构非法在更早阶段直接 400。

服务随主应用一起由 Docker Compose 一键启动（无需额外服务）：

```bash
docker compose up --build          # 然后访问 http://localhost:8000
curl http://localhost:8000/healthz # {"status":"ok"}
```

```bash
# 0. 准备两个源对象（各自修订 1）
curl -si -X PUT --data-binary '0123456789' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/comp/a.txt | grep -iE 'etag|x-revision-no'
# etag: "84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882"
# x-revision-no: 1
curl -si -X PUT --data-binary 'ABCDEFGHIJ' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/comp/b.txt | grep -iE 'etag|x-revision-no'
# etag: "261305762671a58cae5b74990bcfc236c2336fb04a0fbac626166d9491d2884c"
# x-revision-no: 1

# 1. 合成到一个尚不存在的目标：5 个片段按序拼接，同一源可重复取段
#    b[2:5]="CDE"  a[7:10]="789"  a[0:2]="01"  a[7:10]="789"(重复)  b[9:10]="J"
curl -i -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "comp/out.txt",
    "target_baseline": null,
    "media_type": "text/x-composed",
    "segments": [
      {"path": "comp/b.txt", "revision_no": 1,
       "etag": "\"261305762671a58cae5b74990bcfc236c2336fb04a0fbac626166d9491d2884c\"",
       "start": 2, "end": 5},
      {"path": "comp/a.txt", "revision_no": 1,
       "etag": "\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
       "start": 7, "end": 10},
      {"path": "comp/a.txt", "revision_no": 1,
       "etag": "\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
       "start": 0, "end": 2},
      {"path": "comp/a.txt", "revision_no": 1,
       "etag": "\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
       "start": 7, "end": 10},
      {"path": "comp/b.txt", "revision_no": 1,
       "etag": "\"261305762671a58cae5b74990bcfc236c2336fb04a0fbac626166d9491d2884c\"",
       "start": 9, "end": 10}
    ]
  }'
# HTTP/1.1 201 Created
# etag: "ab82ae8e11b54d6ca25f9472686f63c777c26e4d95b434b0dc2cb11c4576baf9"
# x-revision-no: 1
# content-type: application/json
#
# {"target_path":"comp/out.txt","revision_no":1,
#  "etag":"\"ab82ae8e11b54d6ca25f9472686f63c777c26e4d95b434b0dc2cb11c4576baf9\"",
#  "bytes":12}

curl -s http://localhost:8000/objects/comp/out.txt     # CDE78901789J
curl -sI http://localhost:8000/objects/comp/out.txt | grep -i content-type
# content-type: text/x-composed                          <- 用的是请求里的媒体类型

# 2. 带基线继续合成：基线钉住目标当前修订（修订号 + 强 ETag 都要匹配）
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{
    "target_path": "comp/out.txt",
    "target_baseline": {
      "revision_no": 1,
      "etag": "\"ab82ae8e11b54d6ca25f9472686f63c777c26e4d95b434b0dc2cb11c4576baf9\""
    },
    "media_type": "text/plain",
    "segments": [
      {"path": "comp/a.txt", "revision_no": 1,
       "etag": "\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
       "start": 0, "end": 5}
    ]
  }'
# {"target_path":"comp/out.txt","revision_no":2,
#  "etag":"\"c565fe03ca9b6242e01dfddefe9bba3d98b270e19cd02fd85ceaf75e2b25bf12\"",
#  "bytes":5}                                           <- SHA-256("01234")
```

片段顺序就是最终字节序——调换数组里两个元素的位置，合成出的字节与 ETag 随之改变（服务端不做任何排序或合并）。

### 冲突与错误（均为实际响应）

```bash
# 源修订不在库（路径从未发布/修订号不存在/已被 /gc 回收）-> 404，
# 全部命中段按请求顺序列出；目标没有任何修订产生。
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{"target_path":"comp/miss.txt","target_baseline":null,
       "media_type":"text/plain",
       "segments":[
         {"path":"comp/ghost.txt","revision_no":3,
          "etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
          "start":0,"end":1},
         {"path":"comp/a.txt","revision_no":9,
          "etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
          "start":0,"end":1}
       ]}'
# HTTP/1.1 404 Not Found
# {"detail":"source_revision_not_found",
#  "missing":[{"index":0,"path":"comp/ghost.txt","revision_no":3},
#             {"index":1,"path":"comp/a.txt","revision_no":9}]}

# 源 ETag 与钉住的修订不符 -> 412（actual_etag 给出该修订真实强 ETag）
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{"target_path":"comp/se.txt","target_baseline":null,
       "media_type":"text/plain",
       "segments":[
         {"path":"comp/a.txt","revision_no":1,
          "etag":"\"0000000000000000000000000000000000000000000000000000000000000000\"",
          "start":0,"end":2}
       ]}'
# HTTP/1.1 412 Precondition Failed
# {"detail":"precondition_failed",
#  "source_mismatches":[{"index":0,"path":"comp/a.txt","revision_no":1,
#    "expected_etag":"\"0000...0000\"",
#    "actual_etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\""}]}

# 目标基线不符：null 基线但目标已有内容 -> 412（响应头回带当前 ETag）
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{"target_path":"comp/out.txt","target_baseline":null,
       "media_type":"text/plain",
       "segments":[
         {"path":"comp/a.txt","revision_no":1,
          "etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
          "start":0,"end":1}
       ]}'
# HTTP/1.1 412 Precondition Failed
# etag: "c565fe03ca9b6242e01dfddefe9bba3d98b270e19cd02fd85ceaf75e2b25bf12"
# {"detail":"precondition_failed","target_path":"comp/out.txt",
#  "target":{"reason":"expected_absent","current_revision_no":2,
#   "current_etag":"\"c565fe03ca9b6242e01dfddefe9bba3d98b270e19cd02fd85ceaf75e2b25bf12\""}}

# 非空基线但目标已前进到新修订 -> 412，mismatches 列出不符项
# （目标当前是 rev 2/c565...，基线却称 rev 1/ab82...）
# {"detail":"precondition_failed","target_path":"comp/out.txt",
#  "target":{"reason":"baseline_mismatch","mismatches":["revision_no","etag"],
#   "expected_revision_no":1,
#   "expected_etag":"\"ab82ae8e...baf9\"",
#   "current_revision_no":2,
#   "current_etag":"\"c565fe03...bf12\""}}

# 空段 start == end -> 400（即使其它字段都对，也不发布目标修订）
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{"target_path":"comp/empty.txt","target_baseline":null,
       "media_type":"text/plain",
       "segments":[
         {"path":"comp/a.txt","revision_no":1,
          "etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
          "start":4,"end":4}
       ]}'
# HTTP/1.1 400 Bad Request
# {"detail":"segments[0] must not be empty","index":0,
#  "path":"comp/a.txt","revision_no":1,"start":4,"end":4,"length":10}

# 越界 end > 源修订长度 -> 400（end == length 合法，超出才报错）
curl -s -X POST http://localhost:8000/compositions \
  -H 'Content-Type: application/json' \
  -d '{"target_path":"comp/oob.txt","target_baseline":null,
       "media_type":"text/plain",
       "segments":[
         {"path":"comp/a.txt","revision_no":1,
          "etag":"\"84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882\"",
          "start":8,"end":99}
       ]}'
# HTTP/1.1 400 Bad Request
# {"detail":"segments[0] is out of bounds","index":0,
#  "path":"comp/a.txt","revision_no":1,"start":8,"end":99,"length":10}
```

### 源修订钉住与目标锁定的顺序（并发裁决）

合成事务严格按下面的顺序执行，全部成功才提交：

1. **只为目标**`INSERT ... ON CONFLICT DO NOTHING` 补占位行（源路径绝不补——缺失源必须保持 404，且回滚后不能留下占位行）；
2. 用**一条**按路径升序的纯 `FOR UPDATE`（不连接 `revisions`）锁住**全部去重后的源对象行 + 目标行**；
3. 锁内才按 `(object_path, revision_no)` 解析源修订（修订行不可变：源随后再发布、被 `POST /moves` 摘走，旧行内容与 ETag 都不变）、解析目标当前修订；
4. 依次裁决 404 → 源段强 ETag 412 → 目标基线 412 → 空段/越界 400；
5. 逐段读出原始字节，在 Python 侧按序拼接并算 SHA-256，经与 PUT 相同的 `publish_revision` 发布目标新修订（编号走目标自身 `GREATEST(revision_seq, max(revision_no)) + 1`），提交。

为什么不是"源行 `FOR SHARE`、目标行 `FOR UPDATE`"两段加锁？两个锁集交叉的合成（甲锁源 a/SHARE 等目标 b，乙锁源 b/SHARE 等目标 a）会互相等待形成**死锁**；统一为一条按全局路径升序的 `FOR UPDATE` 后，合成与 PUT、`/moves`、复制会话建档（`FOR SHARE` 与本顺序兼容）/确认、`/gc` 执行、租约创建共用同一锁序，只串行、不死锁：

- **并发 PUT 源**：PUT 必须等到合成事务提交才能发布；合成读到的永远是锁前已提交的那个修订的字节，新字节混不进来（HTTP 屏障压测 8 轮，合成体恒为钉住的旧字节）；
- **并发 PUT 目标**：与合成在同一行锁上串行——null 基线下只有一方能创建修订 1，输家醒来得到 **412**；带基线时若 PUT 先提交，合成同样 412；不会出现两个修订 1 或 500；
- **并发 `/gc`**：回收删除前也要拿同样的 `FOR UPDATE`，因此源修订在合成读取期间不可能被删；合成先提交则 `/gc` 的锁内重判定看到新目标修订（`current` 保留），回收先提交则合成在等锁后得到源 404；
- **并发 `/moves`**：迁移不删修订行、只移动 `current_id` 指针，合成按修订号取的是不可变旧行，两端在同一锁序上串行；
- **流式 GET**：GET 的 `FOR SHARE` 与合成的 `FOR UPDATE` 在源行上串行，进行中的流读完后合成才继续，流不被撕裂。

目标与源是**同一路径**时也不特殊：片段按 `(路径, 修订号)` 从**指定的旧修订行**取字节（包括历史修订），新修订要到最后一步才插入，合成读不到自己将要发布的内容。失败的合成整事务回滚：不留目标修订、不推进 `revision_seq`、不留占位行（失败后对该目标 `If-None-Match: *` 首建仍返回 204、修订号从 1 开始）。合成**不**新增任何 `/gc` 保留事实——事务只在持锁期间钉住源修订；提交后新目标修订靠 `current` 保留，源旧修订若不再被别处引用即可照常回收。


## 复制会话（可恢复的快照复制）

复制会话把一组源对象的**某一时刻快照**复制到目标路径前缀下，全过程可重试、可断点续传：

1. **创建会话**：提交源路径集合、目标前缀与可选的上次确认游标。服务在**同一事务**里先对源路径与全部目标路径（目标尚不存在时先插入占位行）按全局路径升序加 `FOR SHARE` 锁，再在锁内快照源修订并为**每个目标路径保存修订身份基线**（`revision_id` 为 NULL 即"目标不存在"）；清单按稳定路径与修订序生成，每条清单项带 SHA-256 内容摘要。清单与基线持久化后源对象发布新修订**不影响**本会话。
2. **拉取分段**：`GET /copy-sessions/{id}/segments` 返回首段；之后用上一段响应里的 `cursor`
   作为查询参数取下一段。每段包含条目内容（base64）、每条目的 SHA-256、段摘要与标识本段的游标。
   重复拉取同一段返回**完全相同的字节、摘要和游标**。
3. **逐段确认**：`POST /copy-sessions/{id}/acks`，请求体 `{"cursor": "<该段游标>"}`。服务将该分段
   对应的修订**作为一次原子批次**应用到目标前缀（目标路径 = `target_prefix + source_path`）。
   确认事务**先按目标路径升序锁定本批全部对象行——包括建档时留下的 `current_id` 为 NULL 的空占位
   行——再读取当前修订**（分两条语句：纯 `FOR UPDATE` 取锁之后才 JOIN `revisions`），然后**与建档基线
   比较修订身份**：
   基线不存在且目标仍不存在则创建修订；基线身份未变且摘要相同则幂等跳过；
   **基线之后目标路径只要发生过发布就是冲突——即使发布的字节与待复制内容完全相同**
   （仅凭摘要无法区分"未改动"与"同字节重新发布"，这是漏判的来源）。
   锁必须先于读、且必须覆盖空占位：确认若先 INNER JOIN `revisions` 再加锁，空占位行会被连接过滤掉、
   一把锁都拿不到，与该路径的首个 `PUT` 交错时双方可能各插一个修订号 1，确认甚至以 **500** 告终而不是
   返回冲突。修复后的两种交错结果：
   **PUT 先提交** → 确认在占位行锁上等待，醒来后看到新修订，返回 `409` 及冲突对象与恢复位置，整批回滚、
   游标不前进；**确认先持锁** → `PUT` 等待行锁，确认提交修订 1 后 `PUT` 接续发布修订 2（`max + 1`，
   不再硬编码 1，也不会死锁）。
   任一冲突则整批回滚，返回 `409` 及结构化结果（冲突对象、`reason`、期望/实际/基线摘要与修订号、
   恢复位置），不留部分应用、不推进游标。批次结果持久化，**重复确认或重放返回同一批次结果**。
4. **恢复**：确认严格按段序进行；游标过期 / 乱序确认返回 `409 cursor_expired` 与恢复位置。
   冲突解决后（例如用条件 `PUT` 修正目标），用结构化结果里的 `resume.last_confirmed_cursor`
   创建新会话即可从上次确认处继续，新会话基于**新的快照与基线**。
   升级前创建、缺少基线的未完成会话，确认未完成段时返回 `409 baseline_missing`（不会按摘要放行，
   也不会推进游标）；用新会话重新建档即可继续。已持久化的历史确认结果仍原样重放。

```bash
# 1. 准备源对象并创建会话（每段 2 个条目）
curl -s -X PUT --data-binary 'alpha' http://localhost:8000/objects/src/a.txt
curl -s -X PUT --data-binary 'beta'  http://localhost:8000/objects/src/b.txt
curl -s -X PUT --data-binary 'gamma' http://localhost:8000/objects/src/c.txt

SID=$(curl -s -X POST http://localhost:8000/copy-sessions \
  -H 'Content-Type: application/json' \
  -d '{"source_paths": ["src/a.txt", "src/b.txt", "src/c.txt"],
       "target_prefix": "backup/", "segment_size": 2}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session"])')

# 2. 拉取首段（响应含内容、每条目 sha256、segment_sha256 与本段 cursor）
curl -s "http://localhost:8000/copy-sessions/$SID/segments" | python3 -m json.tool
CURSOR0=$(curl -s "http://localhost:8000/copy-sessions/$SID/segments" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')

# 源对象随后发布新修订，重拉该段字节/摘要/游标仍完全一致
curl -s -X PUT --data-binary 'alpha-v2' http://localhost:8000/objects/src/a.txt
curl -s "http://localhost:8000/copy-sessions/$SID/segments" | sha256sum
curl -s "http://localhost:8000/copy-sessions/$SID/segments" | sha256sum   # 两次相同

# 3. 确认首段：原子应用到 backup/ 前缀；重复确认返回同一结果
curl -s -X POST "http://localhost:8000/copy-sessions/$SID/acks" \
  -H 'Content-Type: application/json' -d "{\"cursor\": \"$CURSOR0\"}" | python3 -m json.tool
curl -s http://localhost:8000/objects/backup/src/a.txt   # alpha（源快照内容，非 alpha-v2）

# 4. 拉取并确认下一段，直至 has_more == false
CURSOR1=$(curl -s "http://localhost:8000/copy-sessions/$SID/segments?cursor=$CURSOR0" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
curl -s -X POST "http://localhost:8000/copy-sessions/$SID/acks" \
  -H 'Content-Type: application/json' -d "{\"cursor\": \"$CURSOR1\"}" | python3 -m json.tool

# 5. 查看会话状态（complete）与恢复位置
curl -s "http://localhost:8000/copy-sessions/$SID" | python3 -m json.tool

# 6. 冲突示例：目标被独立修改后确认 -> 409 + 冲突对象 + 恢复位置，且整批未应用
curl -s -X PUT --data-binary 'divergent' http://localhost:8000/objects/backup/src/c.txt
SID2=$(curl -s -X POST http://localhost:8000/copy-sessions -H 'Content-Type: application/json' \
  -d '{"source_paths": ["src/c.txt"], "target_prefix": "backup/"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session"])')
C=$(curl -s "http://localhost:8000/copy-sessions/$SID2/segments" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
curl -s -X POST "http://localhost:8000/copy-sessions/$SID2/acks" \
  -H 'Content-Type: application/json' -d "{\"cursor\": \"$C\"}" | python3 -m json.tool
# -> {"status": "conflict", "conflicts": [{"target_path": "backup/src/c.txt", ...}],
#     "resume": {"next_segment": 0, "last_confirmed_cursor": null}}

# 6b. 同内容新修订冲突：建档后目标又发布了一份"字节完全相同"的修订
#     仅比较摘要会误判 already_present 并推进游标；修订身份基线使其返回 409
curl -s -X PUT --data-binary 'delta' http://localhost:8000/objects/src/d.txt
SID3=$(curl -s -X POST http://localhost:8000/copy-sessions -H 'Content-Type: application/json' \
  -d '{"source_paths": ["src/d.txt"], "target_prefix": "backup/"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session"])')
# 会话已创建（基线记录目标 backup/src/d.txt 不存在）；随后独立发布完全相同的字节
curl -s -X PUT --data-binary 'delta' http://localhost:8000/objects/backup/src/d.txt
C3=$(curl -s "http://localhost:8000/copy-sessions/$SID3/segments" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
curl -s -X POST "http://localhost:8000/copy-sessions/$SID3/acks" \
  -H 'Content-Type: application/json' -d "{\"cursor\": \"$C3\"}" | python3 -m json.tool
# -> 409
# {"status": "conflict", "applied": [],
#  "conflicts": [{"target_path": "backup/src/d.txt",
#                 "reason": "target_republished_same_content",
#                 "expected_sha256": "<sha256(delta)>",
#                 "actual_sha256": "<sha256(delta)>",   // 与期望相同，仍是冲突
#                 "actual_revision_no": 1,
#                 "baseline_revision_no": null, ...}],
#  "resume": {"next_segment": 0, "last_confirmed_cursor": null}}   // 游标不推进
# 重新确认重放同一结果；以新会话重新建档（新基线指向当前修订）后则幂等跳过

# 6c. 并发验证：建档时目标尚不存在（空占位行），确认与该路径的首个 PUT 交错。
#     下面的脚本（仅用 Python 标准库）用线程屏障让两个请求同时发出，
#     多跑几次即可观察到两种交错：
#       * PUT 先提交 -> 确认 409（含冲突对象与恢复位置），整批回滚、游标不前进；
#       * 确认先持锁 -> 确认 200 创建修订 1，PUT 等待后接续修订 2（不会重复 1，也不会 500）。
python3 - <<'PY'
import json, threading, urllib.request, uuid

BASE = "http://localhost:8000"

def request(method, path, body=None):
    data = body if isinstance(body, bytes) else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:      # 4xx 也是测试的正常结果
        return e.code, e.headers, e.read()

tag = uuid.uuid4().hex[:12]
src, dst = f"race/{tag}/a.txt", f"backup/race/{tag}/a.txt"
request("PUT", f"/objects/{src}", b"same-bytes")
sid = json.loads(request("POST", "/copy-sessions",
                         {"source_paths": [src], "target_prefix": "backup/"})[2])["session"]
cursor = json.loads(request("GET", f"/copy-sessions/{sid}/segments")[2])["cursor"]

barrier = threading.Barrier(2)
out = {}
def put():
    barrier.wait()
    if PUT_DELAY:                       # 置 0 可偏向"PUT 先提交"的交错
        threading.Event().wait(PUT_DELAY)
    out["put"] = request("PUT", f"/objects/{dst}", b"same-bytes")
def ack():
    barrier.wait()
    out["ack"] = request("POST", f"/copy-sessions/{sid}/acks", {"cursor": cursor})
# PUT_DELAY=0.03 偏向"确认先持锁"（ACK 200 / PUT 修订号 2）；
# PUT_DELAY=0    偏向"PUT 先提交"（ACK 409 / PUT 修订号 1）。两种都应断言通过。
PUT_DELAY = float(__import__("os").environ.get("PUT_DELAY", "0.03"))
threading.Thread(target=put).start(); threading.Thread(target=ack).start()
for th in [t for t in threading.enumerate() if t is not threading.main_thread()]:
    th.join()

put_status, put_headers, _ = out["put"]
ack_status, _, ack_body = out["ack"]
print("PUT:", put_status, "X-Revision-No =", put_headers["X-Revision-No"])
print("ACK:", ack_status)
print(json.dumps(json.loads(ack_body), ensure_ascii=False, indent=2))
assert ack_status in (200, 409)                       # 永远不应该是 500
assert int(put_headers["X-Revision-No"]) == (
    2 if ack_status == 200 else 1)                   # 修订号接续，不重复
PY

# 7. 断点续传：解决冲突后，用上次确认游标创建新会话，从确认处继续
#    （last_confirmed_cursor 取自上一次成功确认的响应 resume.last_confirmed_cursor）
curl -s -X POST http://localhost:8000/copy-sessions -H 'Content-Type: application/json' \
  -d '{"source_paths": ["src/a.txt", "src/b.txt", "src/c.txt"], "target_prefix": "backup/",
       "cursor": "<last_confirmed_cursor>"}' | python3 -m json.tool
```

## 历史修订回收（POST /gc）

对象每发布一次就新增一个修订，历史修订会一直占用存储。`POST /gc` 回收**不再被任何地方引用**的历史修订：

- 请求体：`paths`（1～100 个对象路径，服务去重）、`before`（ISO 8601 时间戳，无时区按 UTC 解释）、`dry_run`（布尔，缺省 `true`）。
- 只有创建时间**严格早于** `before` 的修订才会进入回收视野（出现在 `candidates` / `retained` 中）。
- **一律保留**：对象的当前修订、任何复制会话清单（`copy_manifest`）引用的修订、任何目标基线（`target_baseline`）引用的修订，以及**有效历史修订租约**（`revision_lease`：未释放且 `expires_at > now()`）引用的修订——因此回收永远不会让进行中的复制会话、租约读取或流式 GET 读到悬空引用。
- 任一路径不存在（含只有空占位行的路径）时**整次返回 404**，不回收任何对象。**已迁走的路径**（`revision_seq > 0`）始终算"存在"：历史还在时正常回收，历史被全部清完后重复执行返回空清单（200，与普通对象一致），而其修订号由 `revision_seq` 保留、不复用；只有从未有过内容的空占位行才报 404。
- `dry_run=true`：在可重复读快照中按路径排序返回候选修订号、每路径与总的可释放字节数、被保留修订的原因，**不改动任何数据**。
- `dry_run=false`：按固定路径升序以 `FOR UPDATE` 锁定全部对象行（与发布、建档、确认、租约创建相同的全局锁序），在锁内**重新判定**候选并删除，同一事务提交——任何失败整批回滚，不留部分回收。响应除 `deleted` 外还带 `retained`：锁内重判定后存活的每个修订及其原因（与预览同一组 `current`/`copy_manifest`/`target_baseline`/`revision_lease` 原因），并发 PUT / 建档 / 确认 / 租约创建 / 流式 GET 要么已提交（被锁内重判定看到），要么等待本事务提交，因此在用修订不会被误删。
- 重复执行返回空清单；当前修订（永远是最大修订号）不会被删除，后续发布继续 `max + 1` 递增，**不会复用已回收的修订号**。

```bash
# 1. 准备：发布三个修订（rev 1/2 是历史修订，rev 3 为当前修订）
curl -s -X PUT --data-binary 'v1-aaaa' http://localhost:8000/objects/doc.txt
curl -s -X PUT --data-binary 'v2-bb'    http://localhost:8000/objects/doc.txt
curl -s -X PUT --data-binary 'v3-c'     http://localhost:8000/objects/doc.txt

# 2. 预览（dry_run 缺省即 true）：不改动任何数据
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["doc.txt"], "before": "2099-01-01T00:00:00Z", "dry_run": true}' \
  | python3 -m json.tool
# {
#   "dry_run": true,
#   "before": "2099-01-01T00:00:00+00:00",
#   "objects": [
#     {
#       "path": "doc.txt",
#       "candidates": [
#         {"revision_no": 1, "bytes": 7},
#         {"revision_no": 2, "bytes": 5}
#       ],
#       "releasable_bytes": 12,
#       "retained": [{"revision_no": 3, "reasons": ["current"]}]
#     }
#   ],
#   "total_releasable_bytes": 12
# }

# 3. 执行：同一事务内重新判定并删除，整批成功或回滚
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["doc.txt"], "before": "2099-01-01T00:00:00Z", "dry_run": false}' \
  | python3 -m json.tool
# {
#   "dry_run": false,
#   "before": "2099-01-01T00:00:00+00:00",
#   "deleted": [
#     {"path": "doc.txt", "revision_no": 1, "bytes": 7},
#     {"path": "doc.txt", "revision_no": 2, "bytes": 5}
#   ],
#   "deleted_count": 2,
#   "deleted_bytes": 12,
#   "retained": [
#     {"path": "doc.txt", "revision_no": 3, "reasons": ["current"]}
#   ]
# }

# 4. 重复执行返回空清单；当前修订不受影响；修订号继续递增
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["doc.txt"], "before": "2099-01-01T00:00:00Z", "dry_run": false}'
# {"dry_run":false,"before":"2099-01-01T00:00:00+00:00","deleted":[],"deleted_count":0,
#  "deleted_bytes":0,"retained":[{"path":"doc.txt","revision_no":3,"reasons":["current"]}]}
curl -s http://localhost:8000/objects/doc.txt          # v3-c
curl -si -X PUT --data-binary 'v4' http://localhost:8000/objects/doc.txt | grep -i x-revision-no
# X-Revision-No: 4        <- 不复用已回收的 1/2

# 5. 被复制会话引用的修订一律保留：先建档（清单钉住 rev 1），再发布 rev 2
curl -s -X PUT --data-binary 'alpha' http://localhost:8000/objects/src/a.txt
SID=$(curl -s -X POST http://localhost:8000/copy-sessions -H 'Content-Type: application/json' \
  -d '{"source_paths": ["src/a.txt"], "target_prefix": "backup/"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session"])')
curl -s -X PUT --data-binary 'alpha-v2' http://localhost:8000/objects/src/a.txt

curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["src/a.txt"], "before": "2099-01-01T00:00:00Z", "dry_run": true}' \
  | python3 -m json.tool
# "retained": [
#   {"revision_no": 1, "reasons": ["copy_manifest"]},   <- 清单钉住，不回收
#   {"revision_no": 2, "reasons": ["current"]}
# ]
# 执行也不会删除它们；会话分段拉取仍返回 alpha 快照字节
curl -s "http://localhost:8000/copy-sessions/$SID/segments" | python3 -m json.tool

# 6. 路径不存在：整次 404，不回收任何对象
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["doc.txt", "nope.txt"], "before": "2099-01-01T00:00:00Z"}'
# 404 {"detail":"object_not_found","missing":["nope.txt"]}

# 7. 已迁走路径（POST /moves）：无当前修订但历史仍在，未被会话引用的旧修订照常回收；
#    GET 仍为 404，而再次 PUT 的修订号从旧高水位继续（不复用）。
curl -s -X PUT --data-binary 'm1' http://localhost:8000/objects/mv.txt >/dev/null
curl -s -X PUT --data-binary 'm2' http://localhost:8000/objects/mv.txt >/dev/null
curl -s -X POST http://localhost:8000/moves -H 'Content-Type: application/json' \
  -d "{\"source_path\":\"mv.txt\",\"target_path\":\"mv-dst.txt\",
       \"source_etag\":\"$(printf m2 | sha256sum | cut -d' ' -f1 | sed 's/^/\"/;s/$/\"/')\",
       \"source_revision_no\":2}" >/dev/null
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths": ["mv.txt"], "before": "2099-01-01T00:00:00Z", "dry_run": false}' \
  | python3 -m json.tool     # deleted: mv.txt rev 1 与 rev 2
curl -si -X PUT --data-binary 'm3' http://localhost:8000/objects/mv.txt | grep -i x-revision-no
# X-Revision-No: 3        <- 历史已清空也不复用 1/2
```

## 历史修订租约（POST /revision-leases）

租约把一个**仍在库中的旧修订**（路径 + 修订号）临时钉住，使它在租约有效期内不被 `POST /gc` 回收，并提供一个稳定的读取端点。租约本身不影响 `GET /objects/{path}`（始终服务当前修订）；它服务的是"我需要这个历史修订继续存在一段时间"的场景（长期下载、审计、延迟归档）。

- 请求体：`path`（非空字符串）、`revision_no`（正整数）、`ttl_seconds`（整数，**60～86400**）。
- **只能为仍在库中的修订创建**：修订号不存在、路径不存在（含已被 `/gc` 清空历史的路径）都返回 **404** `revision_not_found`；参数非法返回 **400**。当前修订、以及已迁走路径（`POST /moves`）下尚未回收的历史修订同样在库、可租。
- 成功返回 **201**：随机 `lease_id`（32 位十六进制，每次创建都不同）、绑定修订的 `etag`、以及以**数据库时钟**计算的 `created_at` / `expires_at`（恰相差 `ttl_seconds`）；响应头带该修订的 `ETag` 与 `Last-Modified`。
- 创建事务**先锁对象行（FOR UPDATE）再查修订**，与 `/gc` 执行、PUT、迁移共用同一全局锁序：租约与回收严格串行，不存在"租约返回成功、修订却被删掉"的交错。
- 有效租约是 `/gc` 的第四种保留原因 `revision_lease`，预览与执行都会报告；过期（`expires_at` 已过）或显式释放后即不再保留。
- `GET/HEAD /revision-leases/{id}/content` 对**创建时绑定的旧修订**执行与 `/objects` 完全相同的规则：`If-Match`/`If-None-Match`/`If-Modified-Since`/`If-Unmodified-Since`（304/412）、`Range`/`If-Range`（206/416）。重新发布该路径、迁移该路径、重放同一请求都**不改变**状态码、头部与正文。
- 读取在对象行 `FOR SHARE` 锁内复查租约与修订，事务保持到最后一个字节：**读取开始后租约到期或被释放，正在传输的正文与 ETag/Content-Range 等元数据仍完整一致**；并发的 `/gc` 会等待本次读取提交后才能删除该修订。
- `DELETE /revision-leases/{id}` **幂等**：活动租约本次释放（204），重复释放、释放已过期租约仍是 204；只有从未存在的 ID 返回 **404**。释放/过期后读取内容返回 **410**（`lease_released` / `lease_expired`，响应体带 `path`、`revision_no`、`expires_at`、`released_at`）。注意 410 针对的是**租约状态**：修订可能仍在库中（路径上还有别的租约或它是当前修订），只是这张租约不再允许读取。

```bash
# 1. 准备两个修订（rev 1 是历史修订，rev 2 是当前修订）
curl -s -X PUT --data-binary 'v1-aaaa' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/lease-demo.txt
curl -s -X PUT --data-binary 'v2-current' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/lease-demo.txt

# 2. 为旧修订 rev 1 创建 1 小时租约
curl -i -X POST http://localhost:8000/revision-leases \
  -H 'Content-Type: application/json' \
  -d '{"path": "lease-demo.txt", "revision_no": 1, "ttl_seconds": 3600}'
# HTTP/1.1 201 Created
# etag: "05d099dde10ec92e9d6fbe8345ba9b96f7b62c8ad942aba605b18c5a49151403"
# last-modified: Fri, 02 Oct 2026 13:48:59 GMT
#
# {"lease_id":"13c15a3fe537aea723754b8b6bf07cef",
#  "path":"lease-demo.txt","revision_no":1,
#  "etag":"\"05d099dde10ec92e9d6fbe8345ba9b96f7b62c8ad942aba605b18c5a49151403\"",
#  "ttl_seconds":3600,
#  "created_at":"2026-10-02T13:48:59.881214+00:00",
#  "expires_at":"2026-10-02T14:48:59.881214+00:00"}
# （lease_id 与时间戳每次都不同；ETag 只取决于内容，"v1-aaaa" 的上面这串是确定值。）

LID=13c15a3fe537aea723754b8b6bf07cef

# 3. 参数非法 -> 400
curl -i -X POST http://localhost:8000/revision-leases -H 'Content-Type: application/json' \
  -d '{"path":"lease-demo.txt","revision_no":1,"ttl_seconds":10}'
# HTTP/1.1 400 Bad Request
# {"detail":"ttl_seconds must be an integer in [60, 86400]"}
# revision_no 为 0/-1/1.5/"1"/true、字段缺失或非 JSON 同样是 400；TTL 边界 60 与 86400 合法。

# 修订不在库中 -> 404（路径从未发布，或该修订号已被 /gc 回收）
curl -i -X POST http://localhost:8000/revision-leases -H 'Content-Type: application/json' \
  -d '{"path":"nope.txt","revision_no":1,"ttl_seconds":60}'
# HTTP/1.1 404 Not Found
# {"detail":"revision_not_found","path":"nope.txt","revision_no":1}

# 4. 读取绑定的旧修订：返回的是 rev 1 而不是当前的 rev 2
curl -i http://localhost:8000/revision-leases/$LID/content
# HTTP/1.1 200 OK
# etag: "05d099dde10ec92e9d6fbe8345ba9b96f7b62c8ad942aba605b18c5a49151403"
# accept-ranges: bytes
# content-type: text/plain
# content-length: 7
#
# v1-aaaa

# 条件请求与 Range 规则与 /objects 完全一致（针对绑定的旧修订求值）
curl -i -H 'Range: bytes=0-3' http://localhost:8000/revision-leases/$LID/content
# HTTP/1.1 206 Partial Content
# content-range: bytes 0-3/7
# content-length: 4
# v1-a
curl -i -H 'If-None-Match: "05d099dde10ec92e9d6fbe8345ba9b96f7b62c8ad942aba605b18c5a49151403"' \
  http://localhost:8000/revision-leases/$LID/content          # 304
curl -i -H 'If-Match: "stale"' http://localhost:8000/revision-leases/$LID/content  # 412
curl -i -H 'Range: bytes=99-' http://localhost:8000/revision-leases/$LID/content
# HTTP/1.1 416 Requested Range Not Satisfiable
# content-range: bytes */7

# 5. /gc 预览与执行都报告 revision_lease 保留原因，且不删除钉住的修订
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths":["lease-demo.txt"],"before":"2099-01-01T00:00:00Z","dry_run":true}' \
  | python3 -m json.tool
# "retained": [
#   {"revision_no": 1, "reasons": ["revision_lease"]},
#   {"revision_no": 2, "reasons": ["current"]}
# ]
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths":["lease-demo.txt"],"before":"2099-01-01T00:00:00Z","dry_run":false}'
# {"dry_run":false,...,"deleted":[],"deleted_count":0,"deleted_bytes":0,
#  "retained":[
#    {"path":"lease-demo.txt","revision_no":1,"reasons":["revision_lease"]},
#    {"path":"lease-demo.txt","revision_no":2,"reasons":["current"]}]}

# 6. 路径重新发布或迁移不改变租约响应（/objects 服务新内容，租约端点服务绑定修订）
curl -s -X PUT --data-binary 'v3-totally-different' \
  http://localhost:8000/objects/lease-demo.txt >/dev/null
curl -s http://localhost:8000/objects/lease-demo.txt              # v3-totally-different
curl -s http://localhost:8000/revision-leases/$LID/content        # v1-aaaa（不变）

# 7. 幂等释放：204、再删一次仍 204；释放后读取为 410
curl -i -X DELETE http://localhost:8000/revision-leases/$LID      # HTTP/1.1 204 No Content
curl -i -X DELETE http://localhost:8000/revision-leases/$LID      # 204（幂等）
curl -i http://localhost:8000/revision-leases/$LID/content
# HTTP/1.1 410 Gone
# {"detail":"lease_released","lease_id":"13c15a3fe537aea723754b8b6bf07cef",
#  "path":"lease-demo.txt","revision_no":1,
#  "expires_at":"2026-10-02T14:48:59.881214+00:00",
#  "released_at":"2026-10-02T13:49:00.011964+00:00"}
# 租约到期（超过 expires_at）后读取则是 410 + "detail":"lease_expired"、released_at 为 null。

# 8. 释放后 /gc 才能回收该修订；未知租约 ID 的读取与删除都是 404
curl -s -X POST http://localhost:8000/gc -H 'Content-Type: application/json' \
  -d '{"paths":["lease-demo.txt"],"before":"2099-01-01T00:00:00Z","dry_run":false}'
# ...,"deleted":[{"path":"lease-demo.txt","revision_no":1,"bytes":7}],...
curl -i http://localhost:8000/revision-leases/00000000000000000000000000000000/content
# HTTP/1.1 404 Not Found    {"detail":"lease_not_found", ...}
```

进行中的大文件读取与释放/回收/迁移并发时也是安全的：服务在首字节前对对象行加 `FOR SHARE` 并保持事务到末字节之后，因此读流期间执行 `DELETE` 租约 + `POST /gc`，`/gc` 会等待读取结束；正文一次完整传完后，下一次读取才得到 410、修订才被回收。

## 对象索引（GET /object-index）

按**路径升序**分页列举对象。首次请求（无 `cursor`）建立一份**十分钟快照**：服务在**不取任何行锁**的可重复读事务中遍历当前对象，把每条的 `path`、`revision_no`、`ETag`、`media_type`、`bytes` 复制到独立的快照表；之后携带 `next_cursor` 的每一页都**只回放这份副本**。因此翻页期间发生的 PUT、`POST /moves` 路径迁移或 `POST /gc` 历史回收**不会改变、重复或遗漏**任何一页；建快照本身也**从不阻塞写入**。

每个请求开始时还会顺带清理已过期快照。清理与读页刻意拆成**两个事务边界**：过期 `DELETE` 是页事务**之外**一条独立的读已提交（autocommit）语句，随后的"查存活/取页"才在一个可重复读事务内完成。这样两个请求**同时清理同一份过期快照**时，只在行锁上串行——后拿到锁的一方重新读到提交后的状态、匹配 0 行、删除 0 行，**不会**像把 `DELETE` 放进可重复读事务那样在提交时报 `40001 could not serialize access due to concurrent delete`（旧缺陷会让请求返回 500）；清理方因此不需要任何序列化失败重试。与此同时，读页事务只看到一个数据库快照，清理竞争既不能改变"该快照是否过期"的判定，也不能拆掉一个已判为存活的页：**单页一致性**（过期裁决与页内容属于同一快照）保持不变。并发下的可见结局只有：无游标首查得到**新快照页**；对仍有效快照续页得到**原快照的下一页**；对过期快照续页得到 **410**；非法或与请求不匹配的 cursor 仍是 **400**。

查询参数：

| 参数 | 规则 |
| --- | --- |
| `prefix` | 可选，字面路径前缀（不是按 `/` 分段匹配）；省略或空串表示**全量**命名空间 |
| `limit` | 可选，每页条数；缺省 **50**，强制 **1～100**（`0`、`101`、`1.5`、`01`、非数字均 400） |
| `cursor` | 可选，上一页响应里的 `next_cursor`；签名内含快照 ID、`prefix`、`limit` 与下一页偏移 |

错误：非法参数、伪造/篡改游标、游标与请求的 `prefix`/`limit` 不一致 → **400**；快照超过十分钟（或已被清理）→ **410** `snapshot_expired`，重新发起无游标请求即得到新快照。

```bash
# 0. 先用 Docker Compose 启动（对象索引随主服务一起提供，无需额外服务）
docker compose up --build
curl http://localhost:8000/healthz        # {"status":"ok"}

# 1. 准备几个对象（注意路径顺序与发布顺序无关，索引按路径升序）
curl -s -X PUT --data-binary 'alpha' -H 'Content-Type: text/plain' \
  http://localhost:8000/objects/logs/2026/a.log
curl -s -X PUT --data-binary 'bravo-bytes' \
  http://localhost:8000/objects/logs/2026/b.log
curl -s -X PUT --data-binary 'c' -H 'Content-Type: application/json' \
  http://localhost:8000/objects/logs/2026/c.log

# 2. 首页：prefix=logs/2026/、limit=2（首次请求建立十分钟快照）
curl -s 'http://localhost:8000/object-index?prefix=logs/2026/&limit=2' | python3 -m json.tool
# {
#   "snapshot_id": "7a53377076f847098ff0dc9527cea678",
#   "prefix": "logs/2026/",
#   "limit": 2,
#   "total": 3,
#   "offset": 0,
#   "created_at": "2026-10-03T19:36:38.282818+00:00",
#   "expires_at": "2026-10-03T19:46:38.282818+00:00",   // created_at + 10 分钟
#   "entries": [
#     {"path": "logs/2026/a.log", "revision_no": 1,
#      "etag": "\"8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdda1ed4018e8f2223f8\"",
#      "media_type": "text/plain", "bytes": 5},
#     {"path": "logs/2026/b.log", "revision_no": 1,
#      "etag": "\"a8a381fba7803fda4807a6c1c08906d28b2eb9990a2be74af2dc8f643e272151\"",
#      "media_type": "application/octet-stream", "bytes": 11}
#   ],
#   "next_cursor": "eyJsaW0iOjIsIm9mZiI6MiwicGZ4IjoibG9ncy8yMDI2LyIsInNpZCI6IjdhNTMzNzcwNzZmODQ3MDk4ZmYwZGM5NTI3Y2VhNjc4IiwidiI6MX0.r-7fuxLSOeKNwZSHJXul2Nm_"
# }

# 3. 用 next_cursor 翻下一页（prefix、limit 必须与建快照时一致）
curl -s "http://localhost:8000/object-index?prefix=logs/2026/&limit=2&cursor=eyJsaW0iOjIsIm9mZiI6MiwicGZ4IjoibG9ncy8yMDI2LyIsInNpZCI6IjdhNTMzNzcwNzZmODQ3MDk4ZmYwZGM5NTI3Y2VhNjc4IiwidiI6MX0.r-7fuxLSOeKNwZSHJXul2Nm_" | python3 -m json.tool
# {
#   "snapshot_id": "7a53377076f847098ff0dc9527cea678",   # 同一个快照
#   "prefix": "logs/2026/",
#   "limit": 2,
#   "total": 3,
#   "offset": 2,
#   "created_at": "2026-10-03T19:36:38.282818+00:00",
#   "expires_at": "2026-10-03T19:46:38.282818+00:00",
#   "entries": [
#     {"path": "logs/2026/c.log", "revision_no": 1,
#      "etag": "\"2e7d2c03a9507ae265ecf5b5356885a53393a2029d241394997265a1a25aefc6\"",
#      "media_type": "application/json", "bytes": 1}
#   ],
#   "next_cursor": null                                 # 遍历结束
# }

# 4. 重放稳定：即便期间写入，快照内容不变
curl -s -X PUT --data-binary 'alpha-v2' http://localhost:8000/objects/logs/2026/a.log
curl -s -X PUT --data-binary 'new-file' http://localhost:8000/objects/logs/2026/d.log
# 用上一次的 cursor 重放（含回到 offset=0 的重放），仍是 3 条、a.log 仍是 bytes=5；
# 无 cursor 再请求一次则是新快照（total=4，a.log bytes=8）。

# 5. 全量列举：省略 prefix（或 prefix=）
curl -s 'http://localhost:8000/object-index' | python3 -m json.tool   # limit 缺省 50

# 6. 参数错误 -> 400
curl -i 'http://localhost:8000/object-index?limit=0'
# HTTP/1.1 400 Bad Request
# {"detail":"limit must be an integer in [1, 100]"}
curl -i 'http://localhost:8000/object-index?limit=101'          # 400
curl -i 'http://localhost:8000/object-index?limit=05'           # 400（非规范整数）
curl -i "http://localhost:8000/object-index?prefix=logs/2026/&limit=2&cursor=forged"
# HTTP/1.1 400 Bad Request    {"detail":"invalid cursor"}
# 用别的 prefix/limit 携带旧 cursor 同样 400：
# {"detail":"cursor does not match the request prefix and limit"}

# 7. 快照过期（建立后超过十分钟）-> 410
curl -i "http://localhost:8000/object-index?prefix=logs/2026/&limit=2&cursor=<old-cursor>"
# HTTP/1.1 410 Gone
# {"detail":"snapshot_expired",
#  "snapshot_id":"6f491c4180e344d59f30fdc87fb622bd","ttl_seconds":600}
```

并发清理不会产生 500。下面把 5 份已过期快照放进库里，再让 8 个索引请求在同一屏障后**同时**发出（每个请求都会先执行那条过期 `DELETE`）：它们竞争同一批过期行，但清理是独立的读已提交语句，输家等待行锁后删除 0 行，8 个请求全部正常返回自己的新快照页：

```bash
# 8 个并发请求 + 5 份待清理的过期快照（测试中可直接插入 expires_at < now() 的行）
# 实际观察到的状态码（无 40001、无 500）：
# 8 concurrent requests after seeding 5 expired snapshots
# -> [200, 200, 200, 200, 200, 200, 200, 200]
#
# 并发语义汇总：
#   无 cursor 首查            -> 200，返回一份全新快照的首页
#   有效 snapshot 的续页      -> 200，返回原快照的下一页（同 snapshot_id/offset）
#   过期/已清理 snapshot 续页 -> 410 snapshot_expired（并发清理竞争下亦然）
#   非法 cursor / prefix、limit 不匹配 -> 400
```

快照只是元数据的只读副本（不保存正文、也**不**成为 `/gc` 的保留依据）：历史修订在快照期间被回收、对象被 `POST /moves` 迁走，已冻结的索引页照常返回，实时路径读取则按迁移/回收后的现状响应。

## 本地开发

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://origin:origin@localhost:5432/origin
uvicorn app.main:app --reload
```

## 测试

```bash
pip install pytest httpx pgserver   # pgserver 提供免安装的本地 PostgreSQL
python -m pytest
```

- `tests/test_etags.py` / `test_preconditions.py` / `test_ranges.py`：纯逻辑单元测试（ETag 强弱比较、§13.2.2 求值顺序、范围解析与合并、边界确定性）。
- `tests/test_copy_cursor.py`：复制游标与清单摘要的纯逻辑单元测试（编解码往返、防篡改、确定性）。
- `tests/test_object_index_cursor.py`：对象索引游标的纯逻辑单元测试（HMAC 编解码往返、空前缀/零偏移、确定性、跨密钥拒绝、篡改/垃圾 token 拒绝）。
- `tests/test_object_index.py`：`GET /object-index` 端到端测试，覆盖参数校验（`limit` 缺省 50、边界 1/100、`0`/`101`/小数/前导零/非数字 400、重复参数 400）、字面前缀匹配、空前缀全量、路径升序、条目五字段与页大小/偏移、137 条按 `limit=10` 翻页**不重复不遗漏**、翻页期间 PUT（新增+覆盖）/路径迁移/历史回收后仍回放同一快照（首字节重放一致）、快照不成为 `/gc` 保留原因（历史修订照常删除且冻结页仍可读）、篡改游标与 prefix/limit 不符 400、快照过期或被清理 410（载荷区分并可重新建快照）、TTL 恰为 600 秒，以及并发安全：**DB 层确定性验证建快照事务不持有对象行锁（写者 UPDATE 立即获得，带 `lock_timeout`）**、HTTP 层遍历与 40 个并发 PUT 交错时每页快照一致、无重复无遗漏，以及**过期快照清理竞态**：8 轮、每轮 6 个请求经屏障后同时清理 5 份相同过期快照（含一条对仍有效快照的续页），全部 200 且续页仍返回原快照的精确下一页、过期行与级联条目被清空；8 个请求同时用**过期 cursor** 续页全部 410（无 500/40001）；DB 层确定性验证清理是独立读已提交单元——两个连接并发清理同一过期行均成功（输家删 0 行，而非抛 40001）。
- `tests/test_integration.py`：真实 PostgreSQL + 完整 HTTP 服务的端到端测试，覆盖 200/204/206/304/404/412/416、多段 multipart 确定性、`If-Range`、并发"仅创建"只有一个成功、以及流式传输期间发布新版本不混入内容。
- `tests/test_copy_sessions.py`：复制会话端到端测试，覆盖清单顺序与摘要、分段重复拉取字节一致、源对象发布新修订后仍返回快照内容、原子应用、冲突整批回滚与结构化结果、**建档后同字节重新发布仍判冲突（含已存在目标的同内容新修订、整批回滚、新会话恢复）**、升级前无基线会话返回 `baseline_missing` 不按摘要放行、重放返回同一批次结果、乱序/伪造游标拒绝、跨会话断点续传与冲突恢复、并发确认收敛，以及**确认与目标首次发布在空占位行上的交错：DB 层确定性复现两种先后顺序（PUT 先 -> 409 非 500、修订号不重复；确认先 -> PUT 等待并接续修订号）+ HTTP 屏障压测两者皆不出现 500/重复修订号**。
- `tests/test_gc.py`：历史修订回收端到端测试，覆盖请求校验（路径数 1～100、`before`、`dry_run`）、路径不存在整次 404 且执行路径不删任何修订（整批回滚）、空占位行视为不存在、预览分类与零副作用、截止时间的严格小于语义、路径去重与排序、执行后重放返回空清单、修订号不复用继续递增、清单/基线/租约引用保留与原因合并、混合对象只回收未钉住修订且确认仍可应用，以及并发安全：**流式 GET 期间回收等待且不误删在读修订**、回收与 PUT 交错不删新修订且编号连续、回收与确认交错清单钉住的修订必存活、两个并发执行在锁内重判定收敛（每个修订只删一次）。
- `tests/test_revision_leases.py`：历史修订租约端到端测试，覆盖创建参数校验（路径非空、正整数修订号、TTL 60～86400 且拒绝 bool/float/字符串、边界值 60/86400）、201 响应（随机 32 位十六进制 ID 每次不同、ETag 为绑定修订强 ETag、`expires_at - created_at` 恰为 TTL）、修订不存在/已回收均 404、GET/HEAD 服务**绑定的旧修订**（内容、媒体类型、Content-Length 与 ETag 一致）、条件请求（304/412 针对旧修订而非当前修订）、Range/If-Range/多段 multipart 及 416、重发/重新发布/路径迁移后响应不变、已迁走路径残留历史可租、`/gc` 预览与执行均报告 `revision_lease` 原因且不删除被钉修订（原因与 `current` 合并）、释放与到期后不再保留且读取为 410（区分 `lease_released`/`lease_expired` 与载荷字段）、DELETE 幂等（活动/重复/过期均 204）与未知 ID 404、**2 MiB 流读取期间释放或到期 + /gc 交错时流完整且元数据一致、/gc 等待流结束后才删除**，以及**租约创建与 /gc 在对象行锁上串行：8 轮屏障压测只出现"租约先生效→修订保留且可读"或"回收先提交→租约 404"两种合法结局**。
- `tests/test_moves.py`：对象路径迁移端到端测试，覆盖请求体校验（字段必填、路径非空且不同、强 ETag、正整数修订号）、成功迁移后目标以自身修订号提供相同内容/媒体类型而源 GET/HEAD 为 404、目标编号接续目标自身历史、404（源缺失/已迁走）/412（ETag、修订号或两者不符并回带当前校验值）/409（目标已有内容）均无部分生效、失败回滚后目标仍可 `If-None-Match: *` 首建、**已迁走路径再次 PUT 或作为目标复用时修订号只增不复用、且在 /gc 清空历史后仍由 `revision_seq` 保持单调**、迁移前会话仍可拉取固定源修订且变化的目标基线按原规则 409、/gc 只清理未被会话引用的旧修订而不误删钉住修订，以及并发：同源发散迁移恰一个成功（另一个 404）、争同一目标恰一个成功（另一个 409）、重叠锁集的交叉迁移不死锁且结果为合法串行历史、迁移与目标首发布交错（PUT 先则 409、迁移先则 PUT 接续修订 2，无 500/无重复编号）、流式 GET 期间迁移等待且不撕裂响应。
- `tests/test_compositions.py`：对象合成端到端测试，覆盖请求体校验（非 JSON/非对象、`target_path`/`media_type` 非空字符串、`target_baseline` 必填且只能为 null 或带正整数修订号与强 ETag 的对象、弱 ETag 拒绝、`segments` 1～100、段内路径/正整数修订号/强 ETag/非负 `start`/`end`、`start > end`）、恰好 100 段接受、按序拼接（逆序、同源重复、重叠区间）与字节数/ETag 完全等于 `SHA-256(拼接字节)`、整段与相邻单字节边界、媒体类型取请求值、钉住旧修订（源随后重发不改变结果）、匹配基线成功且编号接续目标历史、已迁走路径残留历史修订可合成；空段与越界 400（`end == length` 合法、首违段索引、回带 `length`）、未知路径/未知修订号/已回收修订号 404（多段按序汇总）、源 ETag 不符 412（回带真实 ETag）、null 基线遇存在目标/非空基线遇缺失目标/修订号或 ETag 不符的各类 412（回带当前 ETag/修订号）、**404 → 412 → 400 裁决优先级**、目标与源同路径时从指定旧修订（含历史修订）取段而失败不改目标、失败不发布修订也不吞编号且失败后目标仍可 `If-None-Match: *` 首建；以及并发：**`/gc` 与合成交错时在用品修订不被删、合成体完整**、回收后新目标以 `current` 保留、两个合成争一个新目标恰一个 201 另一个 412（无重复修订 1、无 500）、合成与 PUT 争目标、**源 PUT 并发 8 轮合成体恒为旧字节不混新**、6 路交叉锁集合成为 201/412 无死锁无重复编号、流式 GET 期间合成等待且流完整、合成与迁移交错只产生合法串行结局。

## 设计要点

- **数据模型**：`objects(path, current_id, revision_seq)` 指向 `revisions(id, object_path, revision_no, content bytea, sha256, media_type, created_at)`；`created_at` 默认 `date_trunc('second', now())`，保证秒级 `Last-Modified`；`revision_seq` 是每路径修订号高水位线，保证路径迁走并清空历史后编号仍单调不复用。
- **事务与锁**：GET/HEAD 在事务内 `SELECT ... FOR SHARE` 锁定对象行并读取当前修订，流式响应在事务提交前持续持有锁；PUT 与批次确认都是**先取对象行锁、再读修订**——先用不含 `revisions` 连接的纯 `FOR UPDATE` 锁定 `objects` 行（`current_id` 为 NULL 的空占位行也会被锁住），锁到手后才 JOIN 读取当前修订并 `max(revision_no)+1` 分配新修订号；并发发布串行执行，前置条件在锁内求值，因此"检查-然后-写"不会交错。
- **空占位行竞态**：建档为不存在的目标留下 `current_id` NULL 的占位行。确认若像旧实现那样"INNER JOIN `revisions` 后对对象加 `FOR UPDATE`"，空占位行会被连接过滤、一行都锁不住：与该路径首个 PUT 交错时双方可能各插修订号 1（唯一约束冲突导致确认 **500**）。修复把**取锁**与**读修订**拆成两条语句——先按路径升序纯 `FOR UPDATE` 锁住包括空占位在内的全部目标行，随后的连接读取不会阻塞、也不会在批次执行期间变陈旧；PUT 侧同样先锁行再读。于是：PUT 先提交时确认等待后看到新修订并报 `409`（冲突对象 + 恢复位置，整批回滚、游标不前进）；确认先持锁时 PUT 等待，确认提交修订 1 后 PUT 以 `max+1` 接续修订 2，双方不死锁。
- **流式隔离**：响应体由 `StreamingResponse` 从被锁定的修订分块读出（`substring(content ...)`），事务在最后一个字节发送后才提交，新修订的发布会等待锁释放，故响应内容、ETag、`Content-Range` 始终属于同一修订。
- **确定性 multipart**：边界 = `SHA-256(ETag | media-type | 升序合并后的区间)`，不含任何随机因素。
- **复制会话快照**：`copy_sessions` + `copy_session_entries` 在创建事务内先对源集与全部目标路径（先 `INSERT ... ON CONFLICT DO NOTHING` 补齐占位行）按全局路径升序 `FOR SHARE` 锁定，再在锁内读取源修订；清单钉住 `revision_id`（修订行永不更新；`/gc` 回收保留一切被清单/基线引用的修订），因此分段内容在会话生命周期内不可变。
- **目标修订基线**：建档时在同一锁快照中把每个目标路径的当前修订身份写入 `copy_session_target_baselines`，目标不存在记 NULL。确认时以修订 `id`（而非摘要）判定是否变更：摘要相同只能证明字节相同，不能区分"自基线未改动"与"基线后又发布了同字节修订"，后者必须报冲突。占位行保证"目标不存在"这一基线也被行锁覆盖，并发首发布会阻塞到建档事务提交。
- **确定性游标**：游标 = HMAC-SHA-256 签名的 `(会话, 段号, 清单摘要, 段末路径, 目标前缀, 源集摘要)`，是同一会话状态的纯函数——重发必相同、跨会话/跨清单必失效（`cursor_expired`）；签名密钥存于 `service_meta` 表，重启后游标仍有效。
- **原子批次确认**：确认在单事务内先用一条纯 `FOR UPDATE`（不连接 `revisions`）按目标路径升序锁定本批**全部** `objects` 行——建档留下的空占位行也在内——再用第二条语句读取当前修订（与建档的 `FOR SHARE`、并发发布及其它会话使用同一全局锁序，无死锁地串行化），逐目标与基线比较修订身份：基线不存在且仍不存在则创建、身份未变且摘要相同则幂等跳过、身份变化（含同字节重发，`reason=target_republished_same_content`）或摘要分歧（`content_diverged`）记为冲突；整批应用 + ack 记录 + 进度推进同事务提交，冲突则整批回滚后单独落库冲突结果。批次结果以**文本**形式存储（保持原始键序），重放与首次响应字节一致；`INSERT ... ON CONFLICT DO NOTHING` 使并发确认收敛到同一条结果。
- **升级兼容**：表结构由启动时的 `CREATE TABLE IF NOT EXISTS` 自动补齐，旧库重启即有基线表；升级前创建的未完成会话没有基线行，确认未完成段时返回 `409 baseline_missing` 且不推进游标（绝不按摘要放行），重新创建会话取得新基线后继续；已经持久化的历史确认结果仍优先重放，不受影响。
- **历史修订回收**：`POST /gc` 的执行路径在单事务内先用一条按路径升序的纯 `FOR UPDATE`（不连接 `revisions`、不补占位行）锁定全部请求对象行——与发布、建档（`FOR SHARE`）、确认共用同一全局锁序，彼此串行化而不死锁——再在锁内重新分类：只考虑 `created_at` 严格早于 `before` 的修订，且当前修订、被任何清单或基线引用的修订一律保留，其余同事务删除，任何失败整批回滚。因为重判定发生在锁内，并发 PUT、建档、确认或流式 GET 要么已提交（其结果对分类可见）、要么阻塞到回收提交，所以在用修订不会被误删、外键（`objects.current_id`、清单、基线）不会悬空。当前修订永远是最大修订号且必被保留，`max(revision_no) + 1` 的编号在回收后仍严格递增、不复用旧号。预览（`dry_run`）则在 `REPEATABLE READ` 只读快照中完成存在性检查与分类，不阻塞任何写者。**路径迁移后**，源路径的对象行仍在但 `current_id` 为 NULL：存在性判定把"`current_id` 非空 **或** `revision_seq > 0`"视为存在，所以迁走的路径（包括历史已被全部回收的）始终是合法回收对象、重复执行返回空清单；只有从未有过内容的空占位行（`revision_seq = 0`）才报 404。
- **对象路径迁移**：`POST /moves` 在单事务内先为两端（按需）插入占位行，再用一条按路径升序的纯 `FOR UPDATE` 同时锁住源与目标的 `objects` 行——与 PUT、建档、确认、回收完全相同的全局锁序，交叉/交换端点的迁移也按同一顺序取锁，串行化而不死锁——锁到手后才 LEFT JOIN `revisions` 读取两端当前修订并按 **404（源无当前修订）→ 412（源强 ETag 或修订号不符）→ 409（目标已有内容）** 顺序判定；全部满足时，把源当前修订的**字节与媒体类型**经 `publish_revision` 作为目标的**全新修订**插入（新的修订 `id`，修订号按目标自身历史 `max+1`），再把源行 `current_id` 置 NULL 摘除——插入新修订与摘除源在同一事务提交，任何失败整批回滚，外部永远看不到部分迁移。迁移不复制修订行本身：它在目标路径新建修订，因此源路径的旧修订行保持原样、仍被迁移前的复制会话清单/基线钉住，`/gc` 据此正常保留。每路径的 `objects.revision_seq` 高水位线在每次发布时随 `revision_no` 一起推进；摘除源行不清零。发布编号取 `GREATEST(revision_seq, max(revision_no)) + 1`，于是已迁走路径（行在但 `current_id` 为 NULL，且其旧修订可被 `/gc` 全部删除）再次 PUT 或作为迁移目标复用时，编号只从旧高水位 +1 继续——绝不回退、绝不复用。
- **对象合成**：`POST /compositions` 在单事务内**只为目标**补占位行，再用一条按路径升序的纯 `FOR UPDATE`（不连接 `revisions`）锁住去重后的**全部源对象行与目标行**——这就是"源修订钉住与目标锁定"的顺序裁决：先取齐一整组有序行锁，然后才解析、核验、读字节。不采用"源 `FOR SHARE` + 目标 `FOR UPDATE`"的两段加锁，因为交叉锁集（甲 SHARE(a) 待 UPDATE(b)，乙 SHARE(b) 待 UPDATE(a)）会互相等待死锁，且 SHARE 不阻塞彼此、无法阻止两段读取之间的发布；单条有序 `FOR UPDATE` 则与 PUT、迁移、复制确认、`/gc` 执行、租约创建落在同一全局锁序上，只串行、不成环。锁内源修订按 `(object_path, revision_no)` 直接查**不可变修订行**（不经 `current_id`），所以源路径的**历史修订、已迁走路径的残留修订、以及目标与源同路径时目标自己的旧修订**都与普通当前修订完全等价地解析；而源路径**绝不插入占位行**，缺失（含已回收修订号）保持 404，回滚也不留任何痕迹。裁决顺序固定为 **404（任一钉住修订不在库，汇总全部命中段的索引/路径/修订号）→ 412（先逐段源强 ETag，再目标基线：null 基线要求目标仍无当前修订，非空基线要求修订号与强 ETag 同时匹配）→ 400（空段 `start == end`、越界 `end > length`，响应带该修订 `length`）**；区间 `[start, end)` 是左闭右开，`end == length` 合法。校验全部通过后才在锁内逐段 `substring(content ...)` 取**原始字节**、严格按请求数组顺序 `b"".join` 拼接（不排序、不合并、不去重），经与 PUT 共用的 `publish_revision` 发布——编号取目标自身高水位、媒体类型用请求值而非源值——与读源、判定在同一事务提交。于是并发源 PUT 的新字节在快照之外且其行锁被持有，混不进拼接结果；`/gc` 与迁移需要同样的行锁，既删不掉在用源修订、也不能让 `current_id` 指针变化影响按修订号的读取；任何 4xx 都在发布前回滚，`revision_seq` 不推进、占位行随事务消失。合成不持久化任何引用、不新增 `/gc` 保留原因：持锁期间对象行锁即钉住事实，提交后仅新目标修订以 `current` 存活。
- **历史修订租约**：`revision_leases(id, object_path, revision_id, revision_no, created_at, expires_at, released_at)` 是 `/gc` 的第四种保留事实，分类 SQL 在锁内多算一个 `EXISTS (... released_at IS NULL AND expires_at > now())`，预览原因记为 `revision_lease`（可与 `current`/`copy_manifest`/`target_baseline` 叠加）。该表**故意不加指向 `revisions` 的外键**：租约过期或释放后修订可以被回收，而租约行必须保留，读取才能回答 410（曾经存在但已失效）而非 404（从未存在）；"租约是否有效"是动态时间谓词，无法由外键表达，一致性改由**对象行锁 + 读取事务持锁到流末**保证（见下）。创建租约在单事务内先用一条**不连接 `revisions` 的纯 `FOR UPDATE`** 锁住对象行（不补占位行——租约不创建对象），锁到手后才按 `(object_path, revision_no)` 查修订：与 `/gc` 执行的"先锁行后分类"在同一全局路径锁序上严格串行——租约先提交则其 INSERT 在 GC 事务快照内可见、修订被保留；GC 先提交则修订已删除、查得 NULL 返回 404，因此"租约成功却丢失修订"在调度上不可达。租约 ID 用 `secrets.token_hex(16)` 随机生成，过期时间由 PostgreSQL `now() + make_interval(secs => ttl)` 计算，与 GC 判定共用同一数据库时钟。内容读取分三步：①无锁读租约行（未知 ID → 404）；②对其 `object_path` 加 `FOR SHARE`；③**锁内复查**租约是否仍未释放且未过期、绑定修订是否仍存在（释放→410 `lease_released`，过期/已回收→410 `lease_expired`），随后复用 `/objects` 的同一套前置条件求值与 Range 计划，并由共享的流式助手在末字节发送后才提交事务。因此一旦读取开始，租约到期、`DELETE` 释放、路径迁移或重新发布都无法改变该响应：GC 的 `FOR UPDATE`/DELETE 会等待 `FOR SHARE` 释放，流完才可能删除；而 HEAD/304/412/416 在同一事务内立即提交。`DELETE` 用 `UPDATE ... WHERE released_at IS NULL AND expires_at > now()` 做条件更新，活动→本次释放、已释放/已过期→幂等 204，仅查无此行才 404。
- **对象索引快照**：`GET /object-index` 首次请求把 `objects JOIN revisions`（当前修订；INNER JOIN 自然排除已迁走的 `current_id` NULL 行与空占位行）按 `path ASC` 遍历的结果**复制**到 `object_index_snapshots(id, prefix, page_limit, total, created_at, expires_at)` 与 `object_index_entries(snapshot_id, ordinal, path, revision_no, etag, media_type, bytes)`。遍历是普通 `SELECT`、**不带 `FOR SHARE/FOR UPDATE`**，运行在独立的可重复读事务中：建快照**不持有任何对象行锁**，因此 PUT/迁移/确认/回收永不被它阻塞，它也不阻塞它们（DB 层用"遍历事务未提交时写者带 `lock_timeout` 的 UPDATE 立即成功"确定性验证）。`object_index_entries` **故意不引用 `revisions`**：快照只是十分钟的**元数据只读副本**，不是第五种 GC 保留事实——历史修订在翻页期间被 `/gc` 删除、对象被 `POST /moves` 迁走，冻结页照常返回，而实时读写/复制/迁移/回收/租约语义完全不变。翻页只查副本（`(snapshot_id, ordinal)` 索引，`ORDER BY ordinal LIMIT/OFFSET`），偏移由游标承载，因此**不重复、不遗漏、可重放**；末页 `next_cursor` 为 `null`，重放末位置返回相同空页。游标 = HMAC-SHA-256 签名的 `(快照 ID, 偏移, prefix, limit)`，密钥独立存于 `service_meta`（`object_index_cursor_secret`，与复制游标密钥隔离），重启后仍有效；服务端先校验签名，再核对游标内 `prefix`/`limit` 与请求一致、快照头存在且选择一致，任一不符 → 400，快照缺失（从未存在或已清理）或 `expires_at <= now()` → 410。`expires_at` 由 PostgreSQL `now() + interval '10 min'` 计算；每次索引请求顺带 `DELETE ... WHERE expires_at <= now()` 清理过期行（条目随外键级联删除）。**清理与读页刻意分处两个事务边界**：清理是取页事务**之外**一条独立的读已提交 autocommit 语句，随后的"取快照头/判过期/取页"才在**同一个可重复读事务**内完成（存活判定与页内容共用一个数据库快照与一个 `now()`）。若把清理 `DELETE` 也放进可重复读事务，两个请求清理同一过期行时会在同一行上都打写标记：后拿行锁的一方在对方提交后做 first-updater-wins 复检，发现自己要删的行已被并发事务删除，提交时报 `40001 could not serialize access due to concurrent delete`，请求 500；拆成读已提交后，输家在行锁上等待、提交后重扫谓词匹配 0 行、删除 0 行，天然幂等、**无需序列化失败重试**。清理事务只能删掉"别的"快照——首查要建的快照在其后才创建，续页所指快照的存活/过期裁决则完全发生在读页事务的冻结快照内——所以清理竞争既不改变过期判定，也拆不掉已判存活的页，**单页一致性**不受影响。TTL（默认 600 秒）、缺省页大小（50）与上限（100）可由 `ORIGIN_OBJECT_INDEX_TTL_SECONDS` / `ORIGIN_OBJECT_INDEX_DEFAULT_LIMIT` / `ORIGIN_OBJECT_INDEX_MAX_LIMIT` 调整。
