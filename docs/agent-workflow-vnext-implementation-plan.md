# Agent Workflow vNext — Incremental Implementation Plan

狀態：Final implementation plan；three-angle independent review approved（99/98/98 confidence）
依據：`docs/agent-workflow-vnext-spec.md`

## 1. Delivery strategy

採 vertical slices；每個 slice結束必須是綠燈、可重跑、可由 artifacts證明的 candidate。vNext使用獨立
namespace與 candidate entrypoint；canonical `SKILL.md`在 canary promotion前維持 accepted legacy contract，
避免兩套互相衝突的 production instructions。

不自動 commit、push、publish、release或修改 local production。Source-writing tests只用 disposable temp repo；
不得對目前 dirty checkout建立 branch/worktree、reset、checkout或 stash。

## 2. Intended source shape

```text
skills/agent-workflow/
├── SKILL.md                              # accepted contract; cutover才原子替換
├── references/
│   ├── vnext-candidate-skill.md          # canary entrypoint
│   ├── vnext-contract.md
│   └── legacy-artifacts.md
├── scripts/
│   ├── workflow_runtime.py               # run-once/admit/run-phase/cancel/reconcile/seal-final
│   ├── phase_protocol.py                 # five lifecycle schemas + sidecar validation
│   ├── process_supervisor.py             # watchdog/PGID/deadline/event drain
│   ├── artifact_store.py                 # create-once seals/fencing/reconciliation
│   ├── baseline_gate.py                  # replayable create-once dirty-tree baselines
│   ├── inspect_legacy.py                 # read-only compatibility CLI
│   ├── run_vnext_canary.py                # stable pre-cutover candidate loader
│   ├── test_vnext_suite.py               # Slice 0a protocol release suite
│   └── test_vnext_candidate.py           # Slice 0b launcher release suite
└── fixtures/vnext/
```

這是 intent，不是預先強制切檔。若同一 executable內的 cohesive classes更清楚，就不為形式拆 module。
只有 `workflow_runtime.py`是新 lifecycle entrypoint；repeating worker seam仍只有 `run-phase`。
第一個 phase另有 `run-once`，把 admission、blocking phase與 cleanup包成一個 deterministic CLI
transaction，避免 coordinator為無語意的 plumbing產生額外 completion。

## 3. Slice 0a — Complete baseline and green contract harness

目的：保護所有既有工作，建立新舊 namespace與永遠綠燈的 negative-contract harness。

工作：

- Create-once `pre_slice_baseline` seals HEAD、branch、replayable staged/unstaged patches、完整 untracked content snapshot
  與 relevant file digests；記錄 Codex/host/model版本。後續 `candidate_gate_baseline`引用它並列出 intended changes，
  不改寫原 seal。`baseline_gate.py`必須能離線驗證 packed bytes、digest與選取範圍。
- 加入兩份 public design docs到 `public-files.json`，恢復 public-tree baseline gate。
- 定義五個 lifecycle contracts與 scoped sidecar taxonomy；不修改 legacy runtime。
- 建立 valid fixtures與 `assert_rejected` negative fixtures：overwrite、schema drift、path traversal、unknown schema。
- 新增 Slice 0a 專屬 `test_vnext_suite.py`並明確掛入 `validate-skill.sh`；用 sentinel證明 release gate真的執行它。
- 凍結 legacy comparison workload corpus、hidden-check manifest與 baseline collection procedure。

Checks：new suite、既有 focused tests、`git diff --check`、`python3 scripts/validate_public_tree.py .`。

Gate：所有 tests綠；negative fixtures因預期 exception綠；baseline manifest可重算且涵蓋 untracked files。

## 4. Slice 0b — Live capability spikes and minimal candidate entrypoint

目的：在建立 writer stack前，先證明 host primitives，不讓 implementer在 Slice 3才做架構決定。

工作：

- 建立最小 `vnext-candidate-skill.md`與 stable `run_vnext_canary.py`；launcher digest-bind candidate instructions與
  Workflow Brief，輸出唯一 native-spawn packet。Slice 1、Slice 6 eval、Slice 8 real canary共用此入口。
- 以獨立 `test_vnext_candidate.py` gate launcher，不讓 Slice 0a protocol suite反向依賴 Slice 0b artifacts。
- Probe Main→`fork_turns=none` Orchestrator transcript exclusion、native inherited top route與 raw-session audit可用性。
- Probe native與external sessions的 host prompt floor：用 `codex debug prompt-input`分類 developer/environment/
  user envelope，並用 exact token events量 cached/uncached input。`fork_turns=none`與 small packet不得代替量測。
- Probe isolated `CODEX_HOME` + sealed named profile的 `codex exec --json --output-schema`：`thread.started`、
  persisted rollout、versioned `turn_context` model/effort、terminal event與 early exit。若
  `--ignore-user-config`會停用 sealed profile，禁止使用並以 exact effective-profile attestation取代。
- Probe inline/named Codex permission profile：repo read、task-root-only write、`.git` read-only、network-off、
  denied external path。Receipt綁 effective profile digest、observed denials、tool/MCP inventory與 env allowlist；若
  current host不能 enforce，writer track立即 blocked。
- 明確拒絕 generic `workspace-write`作 writer evidence；只有 named `[permissions]` profile加 deterministic sandbox
  probe與同 profile `codex exec` observed denial receipt可通過。
- Probe single blocking host adapter、maximum window、early terminal return與 sparse continuation behavior。
- Codex Desktop probe同時固定外層 `functions.exec`與內層 `exec_command` yield window；加入 only-inner-wait
  會回傳 cell handle的 negative evidence，避免把 transport handle誤當 terminal receipt。
- Probe user steer→`cancel` operation→owned process-group signal transport。
- Raw completion measurement從第一個 candidate probe開始，不等完整 accounting slice。

Checks：disposable repo live probes、malicious write/commit/network attempts、route mismatch、35s+ early-return；
pre-cutover `$agent-workflow`仍走 legacy、explicit candidate確實載入、三個 canary/eval surfaces使用同一 launcher digest。

Gate：read-only tracer prerequisites全通過；source-writing prerequisites獨立標 pass/block，不能以 prompt claim代替。
若 host prompt floor無法 source-side 關閉，留下 typed host-owned boundary並讓 canary以 total tokens判斷；不得
假裝 context isolation已等於 token isolation，也不得用任意 worker cap掩蓋。

## 5. Slice 1 — End-to-end read-only routed phase tracer bullet

目的：證明最高價值 seam且不碰 product writes。

工作：

- Implement `admit`、`run-phase`與單次 `run-once`的 read-only path、canonical digests與 create-once seals。
- Pin qualified `top`/`worker` floors與 inherited reasoning；override不得降級。
- 兩個 read-only tasks concurrent launch；capacity不足時同一 phase內排 waves。
- Watchdog直接 drain bounded JSONL/log files，durable bytes、parsed bytes與event count各自 hard-cap；runner stdout terminal-only。
- 實作 read-only canary所需 minimum watchdog、cancel、owned-PGID reap；Slice 2再補 runner-crash durability。
- Named read-only profile以 `:minimal`加唯一 worker root組成；不得繼承會授予 `:root = read`的
  built-in `:read-only`，並以 transient/source credential denial probe驗證實際 policy。
- Bind `thread.started.thread_id`到 canonical rollout `turn_context`，actual route缺失即 fail。
- Atomic task results與 one phase receipt；runner只回 mechanical terminal reason。
- 在 task result／receipt publish前重驗 live repository evidence、runtime bundle與 Codex binary identity；drift一律
  保留 transient evidence但不發布 completed receipt。
- Audit Orchestrator未探索 repo/web/raw logs，且 Main只收到 Orchestrator terminal callback。

Checks：schema/unit、fake process、real CLI route smoke、effective permission attestation、credential denial、
pipe backpressure、exit-0/turn-failed、digest replay、worker count 1/2/4 completion-density comparison。

Gate：每 phase一份 receipt；route 100% attested；target path的 progress/status/wrapper/sparse-wait wake為 0；
single-call probe失敗只能 typed bounded-interim，不能進 default-promotion corpus；所有 owned child terminal。

## 6. Slice 2 — Durable watchdog, deadlines, cancellation and reconciliation

目的：runner crash、timeout、authority revoke都能有界 terminal。

工作：

- Per-task transient watchdog建立 process session/group，持有 task deadline、terminate→grace→kill→wait與 exit receipt。
- Worker先停在 bootstrap release fence；watchdog create-once寫完 immutable active handshake後才准許 exec，消除
  spawn→record orphan window。Active不刪除，由 watchdog或reconciler競爭唯一 terminal receipt關閉。
- Phase Runner不持有匿名 pipes；crash後只依 watchdog receipt、PID/start/command/lock/fence reconciliation。
- Implement `cancel` sealed token與 active PGID signal；cancel後拒絕新 write/integration。
- Queue deadline與 execution deadline：`min(launch+budget, phase, workflow)`；queued expiry=`not_started_deadline`。
- Single-call target與 bounded-sparse transport receipt；sparse continuation不得讀 status/log。
- Create-once generation lease/fencing；contention key固定 predecessor seal + authority revision，winner generation
  ID寫在唯一 claim內；舊 generation所有 dispatch被拒絕。
- Resource capacity：FD budget、log cap、disk floor、backend ceiling、保守 unknown default。
- Deterministic rebuild `view.json`。

Checks：SIGTERM ignored、runner SIGKILL、app restart、stale/PID reuse、cancel during live task、unique audit marker、nohup/double-fork
adversarial case、host-window rollover、full pipe、low FD/disk fixtures。

Gate：所有 owned process groups bounded terminal且無 zombie；觀察到 marker escape時 terminal
`escaped_process_detected`。未觀察不升級成全 descendant guarantee；這是 accepted host limitation。Cancel後零新 action。

## 7. Slice 3 — External-effect containment and isolated source writing

目的：在不競逐使用者 shared checkout的前提下支援 source-writing。

工作：

- Control plane移到 worker readable/writable roots之外；paths以 directory FD、`O_NOFOLLOW`操作。
- Create-once seal：same-filesystem temp、file fsync、atomic no-replace、directory fsync。
- 建立一個 transient isolated execution workspace；host policy若要求 worktree approval，第一次 writer前一次 human gate。
- 從 sealed admission baseline replay HEAD → staged binary patch → unstaged binary patch → 全部 untracked bytes；
  一份 sealed template複製成每個 writer的獨立 workspace，不從 live checkout逐檔取樣。
- 每個 write phase launch前 seal actual write roots、dirty set與 source digests；不在 admission預先封死 dynamic roots。
- Codex permission profile只讀必要 source/packet、只寫 isolated task roots；`.git` read-only、network disabled、
  sanitized env、no plugins/MCP/production credentials。
- Path collision包含 ancestor、realpath/symlink、case-insensitive APFS、Unicode normalization、samefile/device/inode/hardlink。
- Parallel writers只限 enforced disjoint roots；否則 single writer/integrator。
- `run-phase`在 receipt前產生 host-audited bounded patch；同 phase roots必須共用一個 top-level atomic
  integration anchor，否則拆 phase。重驗 shared anchor後以 macOS atomic swap一次安裝，保留 old anchor；
  staging chain以 owner-only directory FD + `O_NOFOLLOW`建立，patch expansion後重查實體 snapshot cap；
  post-swap drift/cancel（含 crash recovery期間）時 swap-back。Intent與terminal create-once，使 swap後、
  receipt前 crash可 reconcile。
- `probe-source-write`以實際 Terra writer、watchdog request/terminal/events、persisted turn context與 deterministic
  macOS sandbox raw stdout/stderr產生 source capability；沒有 producer evidence不得 admission。

Checks：dirty disjoint/overlap、case/Unicode/symlink/hardlink、out-of-root write、`.git/index`、commit/push/deploy、
credential/network probe、two disjoint writers、live source drift before integration。

Gate：worker無 external-effect capability；control artifacts不可篡改；任何 drift/overlap都在 shared checkout write前阻擋。

## 8. Slice 4 — Bounded recovery, expansion and resume

目的：成功 siblings不重跑，失敗 lineage與 dynamic phases都不形成無界 loop。

工作：

- Fixed `collect_all` default；runner只對 enumerated safety facts fail-fast。
- Zero-write idempotent transient infra retry最多一次；writer launch後 failure保存 partial isolated diff，不 blind retry。
- Initial task seal create-once `lineage_id`；criterion/scope digest只作 continuity checks；recovery scope變更仍引用原
  lineage，create-once recovery claim最多一次。
- Workflow只 seal一個 `max_additional_phases`；每個 phase intent與 cause必填，intent是 opaque digest-bound metadata，
  runner不得依內容 branching，Cycle只作 projection。
- ID/name/prompt/scope change不能重置 lineage；new lineage需要 user criterion amendment與 old blocked ref。
- Normal amendment next-boundary apply；cancel/authority/safety走 Slice 2 immediate channel。
- Human gate/app restart後新 generation以 compact resume brief與 active fence恢復。
- Additional Phase的 predecessor等於`caused_by`最後一個 immediately-prior terminal receipt digest；其餘
  named causes也必須 terminal。只計入有 winning generation claim的 plans，loser/orphan plan不得消耗
  expansion budget或出現在 resume brief。
- Watchdog、transient與 process artifacts使用`phase_id/task_id` namespace，讓 task ID跨 phase重用不碰撞。
- Post-swap external edit rollback後 seal `displaced-anchor.json`（含 retained ref/digest或 missing tombstone）；
  `cleanup_allowed=false`且進 resume brief，human resolution前不得清理。
- Generation claim獲勝但 lineage claim/request/watchdog尚未落盤時，deterministic reconcile補回同一份 claim，
  或在 request/claim/process/marker證據一致時以 `not_started_interrupted` terminalize；plan-before-claim orphan
  不進 authoritative reconcile。Cancel fence在 normal與 reconcile publication都同樣生效。
- Resume/amendment seal先以 identity-bound process reconciliation證明沒有 active attempt；偽造 terminal file、
  stale instruction boundary、cross-workflow amendment、retained-tree drift、authority drift與 cancel都 fail closed。
  Existing terminal必須重驗 request/active/log digests與 worker/marker liveness，不能以 file existence代替 proof。
- Criterion amendment的 blocked result必須屬於 latest terminal phase；不得用歷史 failure跨過已完成 phase後才
  回溯推進 authority，確保 runtime acceptance與 final replay時間序一致。
- Final replay接受合法 historical generations，但每個 receipt都綁 exact generation claim；authority revision依
  create-once amendments逐 phase邊界推進，final generation必須擁有 verification phase。

Checks：single sibling fail、shared precondition fail、safe retry、partial writer、recovery success/fail、ID bypass、
criterion/instruction amendment、mixed sibling outcome、expansion exhaustion、old/new generation race、plan/claim/
lineage/request/watchdog write-boundary crash、cancel-before-reconcile-publication、forged terminal、retained-tree drift、
multi-generation replay。

Gate：每 stable lineage最多一個 autonomous recovery；successful siblings不重跑；exhaustion只會 final/block/human gate。

## 9. Slice 5 — Independent verification and deterministic final seal

目的：quality proof與 final ownership可執行。

工作：

- Read-only clean-packet `top` Verification Phase；identity不得等於任一 writer，route不得低於 qualified floor。
- Verifier輸出 criteria coverage、evidence/commands、P0-P3與 confidence；P0/P1必修，P2 typed resolution。
- Repair後 mandatory reverify。
- Orchestrator產生 bounded final candidate；`seal-final`驗證 all-terminal、verification、authority與 fence後
  create-once `final.json`。
- Final replay逐 task綁 exact lineage origin/recovery sidecars；complete只接受 chain最後一個 independent top/read
  decision、high-confidence pass、完整 criteria、P0/P1=0、P2 finding/resolution exact set、target completion density。
- Candidate先 replay、再 create-once publish；既有 final拒絕 overwrite，且 final後 phase/cancel/amend/resume/
  reconcile mutation全部 fail closed。
- Workflow root directory OS lock採 shared mutation／exclusive finalization；crash自動釋放。Deterministic
  interleaving fixtures須覆蓋 phase、cancel、amend、resume、reconcile與 final，不新增 durable state。
- Verifier independence除 task/lineage外也比較 actual routed session identity；任何 prior worker/writer session
  即使改名也不能成為 final verifier。
- Raw host audit驗證 Main callback後只 delivery；portable v1不謊稱 hard tool disable。

Checks：writer self-approval、identity collision、downward route override、missing criteria、P2 variants、repair/reverify、
final overwrite/crash、Main post-final repo/tool action negative canary。

Gate：沒有 independent qualified top receipt不可 pass；final可重播驗證；Main-delivery violation使 production claim fail。

## 10. Slice 6 — Thin-skill polish and frozen behavior eval

目的：模型依 first principles自主規劃，不把 runtime手冊塞回入口。

工作：

- 只修改 candidate skill：explicit invoke、Clean Orchestrator、dynamic phases、one runner seam、context/audit、
  independent verification、human gates與 stop rules。
- Detailed schema/CLI procedures放一個 on-demand reference；移除 fixed lanes/rounds、two-agent cap、manual fallback。
- Bilingual guide說明 external workers不在 native tree、`view.json`是唯一 guaranteed UI。
- Skill eval比較 candidate vs no-skill baseline；評分 plan correctness、context hygiene、completion density、authority。

Checks：skill lint、trigger/negative-trigger、adversarial plan eval、brief size、with-skill vs baseline blind review。

Gate：candidate入口 lean；所有 eval符合 routing mandatory、phase dynamic、no polling、quality-first與 external-action boundary。

實作狀態（2026-07-12）：candidate為253 words，詳細 mechanics集中在單一549-word runtime reference；canonical
legacy `SKILL.md`尚未切換。Frozen label-neutral corpus覆蓋 explicit multi-phase、budget-first adversarial、negative
small edit與 failed-sibling recovery，固定評分 plan correctness、context hygiene、completion density、authority。
CLI範例另以 parser contract測試綁定；曾發現錯誤的 `run-phase`／`cancel`參數並以 red→green fixture修復。
此 deterministic corpus與單次 clean forward review只作 Slice 6設計 evidence，不替代 Slice 8真實 paired blind canary。

## 11. Slice 7 — Accounting and observability enrichment

目的：精確 routed hot path，誠實標 native post-turn coverage，不製造 late-seal wake。

工作：

- External tasks從 terminal events/rollouts精確記帳，綁 task/attempt digest。
- Native post-turn adapter：App Server `thread/tokenUsage/updated`優先；version-gated Stop hook parser fallback。
  Hook transcript格式官方標為 unstable，schema drift必須降為 partial。
- Workflow boundary到 Orchestrator terminal；Main delivery另列。
- Semantic final只宣告 pending；post-terminal sidecar才 seal token與 completion density。
- App exact同時需要 ordered turn start/usage/successful completion；單一 usage event不得冒充完整 terminal boundary。
- Completion classifier直接重播 raw session，不接受 caller-authored labels；拒絕 wrapper/status/partial-progress wakes。
- Sidecar綁 current runtime bundle、raw native/session evidence與 derived projection；crash／lost response可 replay。
- Legacy parser只作 read-only diagnostics。

Checks：App Server terminal replay、hook session/turn/path drift、missing coverage、arithmetic/digest tamper、
caller-label bypass、forbidden completion、bundle drift、crash-before-sidecar與 lost-response idempotence fixtures。

Gate：每個數字標 exact/partial source/confidence；final不需要額外 model wake補帳。

## 12. Slice 8 — Frozen canary, cutover, legacy reader and rollback drill

目的：quality-first證明後才一次切 default。

工作：

- Slice 4–7只跑 focused deterministic tests、disposable-repo fault injection與 milestone repository gates；
  不因 executable runtime每次變動重跑昂貴 live source qualification。先前 live probe只對它的 exact bundle
  提供 historical evidence，不作 promotion evidence。
- Slice 8所有 source-owned executable seam（包含 canary evaluator、legacy reader與 pinned-bundle rollback）
  完成 deterministic tests與獨立 review後，才明確宣告 executable bundle freeze（runner、protocol、routing、
  source/recovery/final/accounting/canary/legacy executables與其 schema/fixture digests）。Freeze後立刻只跑一次
  新的 authoritative live qualification，再進
  paired canary。任何 executable change都使該證據失效，必須記錄 deliberate requalification decision。
- 只有無法用 deterministic fixture驗證的新 host primitive可在 freeze前跑 exploratory live probe；它必須
  清楚標為 exploratory，不能被 final/canary/promotion gate引用。
- Versioned corpus：read research、multi-writer、single integrator、failure/recovery、long verification；每 workload至少5 paired trials。
- 在 vNext揭盲前 freeze paired order、repo/host/model/reasoning/capacity、hidden-check hashes與 blind rubric。
- Hard gate：all invariants/authority 100%、no hidden regression、P0/P1=0、每 P2 typed、blind review無 material regression。
- Noise fraction=`1.4826×MAD/median`。Hard gate後 performance：median completions -50%；median tokens降低
  `max(20%, 2×noise fraction)`；每 workload paired median latency ratio≤1.10且 aggregate≤1.00；P95只報告。
- Implement `inspect-legacy` read-only CLI，support frozen v1/v2 fixtures、corrupt/unknown/path traversal且 zero writes。
- Admission pin runtime bundle digest；default rollback後 active run用 pinned bundle resume，否則 typed incompatible block。
- Admission在 `workflow.json` 尚未 commit時可 exact-byte repair partial pin；commit後缺檔或 drift只能 typed block。
- Promotion freeze同時 seal executable與 semantic candidate（candidate instruction、protocol/canary/legacy fixtures）；
  paired seed與 balanced labels只能由 corpus ID + semantic digest推導。
- Canary run seal綁 Codex version與 App protocol digest。Coordinator tokens逐 response累加 App Server `last`並
  對齊 cumulative `total`；completion count只接受 raw cumulative delta恰等於 `last_token_usage`的同 terminal
  boundaries。每個 workload有 sealed unique worker-session floor，continuation不能冒充額外 worker；receipt保留 unique session並以 ordered
  `continuations`表示同一 lineage的唯一 recovery。每個 attempt另以 canonical launch packet綁
  pair/variant/task/role/route/transport/full prompt；vNext CLI worker tokens由 pinned-version `codex exec --json`
  terminal usage重播，legacy worker使用 App Server。App turns、token updates與 model completions不得互相替代；
  host authority另 seal每個 variant的 canonical launch manifest；每筆含 session、attempt ordinal與 turn ID，
  ordered attempt集合必須與 receipt完全相等。Initial `codex exec`與 App Server continuation分開 replay；後者
  cumulative total必須扣除前一 turn的 digest-bound breakdown，raw final breakdown再逐欄等於所有 attempts總和；
  raw launch的 final explicit message只能包含唯一的 sealed `input_text` part；只允許 pinned、完整封閉的
  environment-only或 AGENTS+environment host preamble。因 host只在 launch時產生隨機 `codex-arg0<token>` basename，
  session/reviewer/verifier create-once launch packet pre-bind的是只正規化該 basename的 canonical digest；穩定 path
  prefix與其餘 bytes仍需 exact match，未正規化 raw/native copy則保留為 post-launch byte authority。Hidden proof移除 caller-authored facts，改綁 per-subject typed replay record；host qualification HMAC
  seal完整 record set與 frozen qualification command records；reviewer/verifier packet內嵌 workload、可讀且
  digest-bound的 output、rubric、hidden evidence。Contract-specific fault matrix涵蓋 path traversal/overlap、terminal、
  denial、recovery、early/wrong verification、watchdog tamper/missing/duplicate、artifact replay與 delivery；
  每個 hidden check使用不同的
  deterministic validator facts，blind reviewer與 independent verifier的 raw launch packet綁實際 evidence/output bytes。
- `seal-run`在 worker root外建立 per-run 0600 replay key，保留至 host archival/cleanup；`seal-results`先
  重新驗 frozen manifest與目前 repository executable/authority bytes，再 deterministic replay unsigned draft、
  從 `seal-run`建立的 exact-byte read-only host frozen repository執行 qualification scripts，最後 HMAC-seal完整
  transitive evidence graph；不得從 mutable checkout執行。Codex raw/App events/
  repository snapshot/hidden proofs在 worker workspace的 copy都必須 byte-match run-specific canonical host store。
  Canonical store必須 host-owned mode 0700，且每個 raw turn的 effective managed restricted permission profile
  都不得涵蓋任一 canonical root。P2 reverify/gate另綁 exact qualification command record digest。
  Qualification receipt本身以 host authority HMAC簽署；crash／response loss後跨 process retry重用 exact receipt，
  不重跑 timing-variable test output。Unsigned／post-seal modified results不可 promotion；不需常駐 service。
- Candidate版本=`1.0.0-rc.N`；Canary pass後以 `1.0.0` atomic replace canonical `SKILL.md`與 default，並同步
  registry/changelog/package。Legacy writer永不 fallback；reader留至 `<2.0.0`，最早2.0另批移除。

Checks：blind adjudication、rollback during active run、app restart/resume、same source digest、legacy compatibility matrix、
`bash scripts/validate-all.sh`、`bash scripts/preflight.sh`。

Gate：correctness與 authority先過；routing/isolation/reap 100%；forbidden wakes 0；performance/token thresholds通過。

Human Gate順序：source canary → commit approval → local production approval + same-digest smoke → publish approval。
Portable runtime只輸出 pending-action handoff；approval record/action executor由 Codex host擁有。Dirty-overlap、
worktree、commit、local production、publish approvals彼此不可重用或擴張。

實作狀態（2026-07-12）：freeze v5 live qualification後的 25-pair run完成，但 independent verifier因 inline 320份
完整 proofs形成 995,367-byte packet而耗盡 context；v5因此明確失效，只保留 historical evidence。其 deterministic
repair改用 create-once digest-bound proof manifest與 verifier-readable copies。該 run也暴露 bounded-recovery用 fresh
worker session造成 latency regression；CLI resume雖快但不 append authoritative rollout，因此已改為短命 App Server
exact-session adapter。Crash-after-append replay、cwd/permission attestation、transport/adapter/Codex digest fence，以及
`codex exec` initial turn + App Server continuation的 exact accounting均已有 deterministic fixtures。尚待本輪獨立
review、repository gates、real verifier readable-proof smoke，之後才宣告新 freeze、跑唯一 authoritative live
qualification與新的 paired canary。

後續freeze v6完成25-pair sealed canary：completion median `7→1`、token median降低31.92%、blind quality持平，
且exact-session recovery workload latency ratio改善至0.884；但aggregate latency ratio為1.020，single-integrator
為1.122、long-verification為1.647，因此不promotion。Raw session分解顯示slow path來自`codex exec` Terra worker
自行啟用skills／shell探索，而非coordinator或top verifier。下一個repair slice先以固定model-visible isolated-worker
developer contract阻止unrequested exploration；同時修正paired evaluator只把legacy-only `AW-H003`視為已知
baseline defect，其他legacy hidden failure與所有vNext hidden failure仍fail closed。任何修後bundle都必須重新freeze、
live qualify與paired canary，不得沿用v6 evidence。

Slice 9 isolation repair已用兩個non-promotion exploratory probes驗證：明確符合`skill-creator` trigger的Terra Task
仍為零skill read／零tool／零delegation；同一session經production `app_resume_adapter`續跑skill-matching recovery亦為
零額外action。Recovery probe先以`resume prompt authority drifted` fail closed，揭露現行App Server會把AGENTS與
environment materialize成同一host user message的兩個`input_text` parts；新增failing fixture後，adapter只接受完整
typed envelope組合，仍拒絕額外user authority。`AW-H003`另有legacy-only、vNext-only、雙邊與非density legacy矩陣。
這些都不是promotion evidence；repository gates與independent GO後才可宣告下一個 executable freeze。

最終freeze v7與唯一authoritative live qualification均通過。全新25-pair canary在correctness零failure、blind
quality兩邊皆4/4/4下通過performance gate：completion median `7→1`（-85.71%）、token median
`199,987→84,588`（-57.70%）、aggregate latency ratio 0.634；五類workload ratios依序為0.271、0.786、
0.698、0.525、0.377。Post-canary freeze replay仍一致。Source candidate完成；commit、local production、
publish/default cutover仍是分離的human-owned gates，未被本輪授權。

## 13. Verification matrix

| Layer | Every slice | Before cutover |
|---|---|---|
| Static | compile、schema/lint、`git diff --check` | public/secret scan |
| Unit | protocol、artifact、path、deadline、lineage | full unit suite |
| Integration | fake supervisor + temp repo | real CLI/routing/permission profile |
| Fault | slice-specific injection | crash/restart/cancel/rollback matrix |
| Skill | candidate trigger/plan eval | frozen blind comparison |
| Runtime | deterministic macOS disposable-repo fault probes | post-freeze authoritative live qualification + same-workload ≥5 paired trials |
| Repository | focused + wired vNext suite | `validate-all` + `preflight` |
| Release | none without approval | dry-run local release + same-digest diff |

每個 slice保存 changed files、commands/exit、deterministic/fault evidence、open P0-P3、completion/token/latency
observation；歷史或 exploratory live probe必須同時標 exact bundle與 non-promotion status。

## 14. Review resolution ledger

所有 first-round與 second-round P0/P1均在 spec/plan修正。P2 typed resolutions：

| Finding | Resolution |
|---|---|
| Five types忽略 sidecar authority | fixed：改稱 lifecycle-state contracts並列 scoped authoritative sidecars |
| Runner回 next semantic reason | fixed：只回 mechanical `terminal_reason` |
| Pipe backpressure | fixed in Slice 1：watchdog direct drain + bounded files + incremental parser |
| Wave deadline不明 | fixed in Slice 2：queue deadline與 min execution formula |
| Legacy completion parser誤作 oracle | fixed：legacy diagnostics only |
| Baseline漏 staged/untracked | fixed in Slice 0a |
| Expansion/lineage owner不清 | fixed：single max-additional-phases + original sealed lineage；semantic rename由 verifier補足 |
| Approval順序與隔離 | deferred_with_owner_gate：host owner；commit → local production → publish；portable只輸出 pending-action handoff |
| Public docs未 allowlist | fixed as Slice 0a first repository change |
| Host resource admission | fixed in Slice 2：FD/log/disk/backend caps；unknown conservative |
| Admission profiles與 slice ordering | fixed：probe/read-only/source-write矩陣；Slice 1含 minimum watchdog/cancel |
| Generation contention key | fixed：predecessor seal + authority revision唯一 no-replace claim |
| Escaped daemon detection | accepted_with_rationale：unique marker scan可發現則 fail；claim只涵蓋 owned PGID |
| Permission profile requested/effective drift | fixed：receipt綁 effective digest、denial probes、tool/MCP/env inventory |
| Sparse wait與 zero-wake衝突 | fixed：獨立 bounded-interim class，不得進 target promotion corpus |
| Baseline會被後續 edits覆蓋 | fixed：create-once pre-slice + referencing candidate-gate baselines |
| Opaque `intent` field | fixed：唯一 opaque digest-bound audit metadata；runner禁止 branching |
| Wall vs monotonic deadline | fixed：duration seal→process monotonic；restart依 elapsed/remaining reconciliation |
| 0.3.4到 legacy reader retention | fixed：1.0.0-rc.N candidate、1.0.0 cutover、reader保留到<2.0.0 |

## 15. Stop and rollback rules

立即停止 autonomous implementation並回報 blocker：

- Clean Orchestrator transcript exclusion或 qualified top attestation不成立；
- persisted external route attestation、permission profile、cancel transport或 owned-PGID supervision不成立；
- source-writing需要 shared-checkout race或修改/丟棄 user changes；
- correctness/authority canary失敗且當前 slice無法修復；
- 需要 worktree、commit、local production或 publish authority。

Rollback不 dual-write。未 cutover前停用 candidate entrypoint；cutover後 selector只影響新 run，active run使用
admission-pinned runtime bundle，並保留 immutable artifacts供 diagnostics。
