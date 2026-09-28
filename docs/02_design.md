# Template Design Specification

## Position in AgentCore Architecture

- **Agent Class**: DealScoringPipelinePriorityGraph
- **L1 Base**: AgentBaseGraph（**L1 Base** を直接継承。AutonomousBaseGraph は使わない）
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

本テンプレは Cat 2 オーケストレーターであり、外側は `AgentBaseGraph` の固定 5 ノード backbone
（initialize → pre_process → main → post_process → finalize）に従う。ドメインのマルチステップ
（停滞検出・決定論スコア・優先度付け・LLM 定性抽出）は `main` スロットに置いた `GraphNode` が内側の
`domain_workflow_graph`（`src/graph/domain_workflow_graph.py`）として encapsulate する。旧 Level 2 base 継承
モデル（RAGAgent 等の L2 型を継承する方式）は本リポジトリでは廃止され、**L1 Base を直接継承**する。

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema_version / session_id / trust_level を設定 | — | メタ初期化 | InitializeNode (default) |
| pre_process | `DealRepository` で SQLite の `deals` テーブルを取得（欠損許容）。スコア因子と `notes` のみ残し PII カラムをドロップ。対象案件リストと欠損マップを構築 | `input_context`（任意フィルタ: 期間/担当/フェーズ） | `deals`, `missing_map` | FunctionNode（`execute` + `_extra_security_gate_input()` override） |
| main | 内側 `domain_workflow_graph` を実行（下記 4 サブノード）。GraphNode として上流 `status="error"` はパススルー | `deals`, `missing_map` | `scored_deals`, `qualitative` | GraphNode（`get_subgraph()` / `extract_input()` / `merge_output()` override） |
| post_process | 決定論スコアと LLM 所見を分離・欠損多数行に「要手動確認」フラグ付与・S-3 出力チェック・Markdown レポート生成 | `scored_deals`, `qualitative` | `output`（Markdown） | FunctionNode（`execute` + `_extra_security_gate_output()` override） |
| finalize | response_metadata / total_time_ms を構築 | — | 応答メタ | FinalizeNode (default) |

#### 内側 domain_workflow_graph のサブノード（main の GraphNode が実行）

| サブノード | 種別 | 責務 |
|---|---|---|
| StagnationDetectNode | 決定論 | 先頭で `user_input` を `json.loads` して `deals`/`missing_map`/`input_context` を展開。`last_updated` からの経過日数で停滞判定（既定 14 日超で停滞フラグ） |
| ScoringNode | 決定論 | 取得できた因子のみで受注確度スコアを算出（フェーズ×金額×経過日数×BANT 充足率）。因子カバレッジ（N 因子中 M）を併記 |
| PriorityDecisionNode | 決定論 | `routing_table` 方式（Financial Document Classification & Routing Agent 参照）でルールベース優先度ランク付け |
| QualitativeAnalysisNode | LLM | `notes` の自由記述からスコアに現れない定性リスクを抽出し、案件別の推奨アクション・コーチングコメントを生成 |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry)
                                          pre_process

  [main 内側 domain_workflow_graph]
    StagnationDetect → Scoring → PriorityDecision → QualitativeAnalysis
```

> **route の契約（SDK 1.0.0）**: `pre_process → main` は無条件辺。`route()` は main 実行後に
> `state["status"]` を読む（`success`→post_process / `retry`→pre_process / 他→finalize）。
> `add_edges()` / `route()` は override しない。main は上流 `status="error"` を無条件でパススルーする
> （pre_process が DB 取得失敗で error を立てた場合、main の GraphNode は再実行せず error を通す）。

### State Definition

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `input_context` | `dict` | 呼び出し側からの絞り込み条件（期間・担当・フェーズ等。任意） | No |
| `deals` | `list[dict]` | `deals` テーブルから fetch し Contract 準拠検証を通過した対象案件（スコア因子 + `notes`・primitive のみ）。0 件もあり得る（下記「空 deals の分岐」参照） | main まで（0 件可） |
| `non_conformant` | `bool` | Contract 非準拠（全行 `deal_id` 欠落）を検出したフラグ。true のとき main は起動するが内側が 0 件で即戻る（後述） | No |
| `missing_map` | `dict` | 案件 ID → 欠損因子リスト | Yes |
| `scored_deals` | `list[dict]` | スコア・優先度・カバレッジ・停滞フラグを付与した案件 | Yes（post まで） |
| `qualitative` | `dict` | 案件 ID → {定性リスク所見, 推奨アクション}（LLM 出力） | No |
| `output` | `str` | 最終 Markdown レポート | Yes（finalize） |
| `status` | `str` | AgentStatus 値（"success" / "error" / "retry"） | Yes |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- InvocationContext via `config["configurable"]` only (not in State)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

> **DB 接続情報の扱い**: SQLite ファイルパスは `current_secrets().require("DEALS_DB_PATH")`
> （InvocationContext に bind された `SecretProvider`）経由で取得し、
> **State には一切格納しない**（State は checkpoint DB へ永続化されるため、credential in State =
> credential in plaintext）。`os.environ` は直接参照しない。Agent の実行経路は**読み取り専用参照**で、
> 書き戻しの副作用を持たない。

### `deals` テーブル・インターフェース仕様（本 AG が要求する読み取りスキーマ）

> 本 AG がスコアリングに必要とする **`deals` テーブルの列**を定義する。これは営業支援クラスタの
> **共通 Data Contract（正本 = Enterprise Sales Support Data Foundation Builder Agent リポ `docs/data_contract.md`・contract_version 1.0.0）** への参照側
> インターフェース要件であり、契約の相手は Data Contract（スキーマ）であって Enterprise Sales Support Data Foundation Builder Agent という AG 実装
> ではない（2026-07-03 クラスタ方針確定）。テーブルの整備経路は 2 つを許容する —
> **経路(a)**: 基盤 AG Enterprise Sales Support Data Foundation Builder Agent が非定型ファイルを分類・正規化して整備（任意のセットアップヘルパー）、
> **経路(b)**: 顧客が Data Contract 準拠の `deals` テーブルを直接用意（既存 SFA/CRM エクスポート等・
> Enterprise Sales Support Data Foundation Builder Agent 不要）。いずれの経路でも本 AG の動作は同一（列名ズレは pre_process の列名マッピング
> config で吸収する）。
> 本 AG は `DealRepository` を通して読み取る。現行 backend は SDK の `BaseDatabase` を実装した
> `SQLiteDatabase` であり、SDK 1.0.x の PostgreSQL / MySQL STUB は使用しない。SQLite は初回構築時に
> 本表に対応する `deals` スキーマと owner / phase / last_updated の index を `IF NOT EXISTS` で作成する。
> `:memory:` は named shared-cache URI と keeper connection で操作間の内容を保持し、各接続には
> `busy_timeout=30000` を設定する。書き込みは operation 単位で commit/rollback し、複数操作が必要な場合は
> `transaction()` が同一 connection 上で `BEGIN IMMEDIATE` から commit/rollback までを区切る。
>
> **欠損許容の原則**: 下表で「スコア寄与」の列が欠けていても行はスキップせず、その因子だけカバレッジから
> 除外してスコアを算出する（`missing_map` に記録し、欠損多数行は post_process で「要手動確認」フラグ）。
> 必須は**行を識別できる最小限**（`deal_id`）のみ。

> **正本準拠の凡例（必須列の扱いを経路で分ける）**: 「必須（正本 1.0.0）」列は Data Contract の NULL 可否に
> 一致させる。**経路(a)（Enterprise Sales Support Data Foundation Builder Agent 整備）のデータは正本保証により必須列が揃っている前提**で、本 AG は
> それを信頼する。**必須列の欠損許容は経路(b)（顧客が直接用意し正本の品質検証を経ないデータ）に対する
> 防御**であり、経路(b) で必須列が欠けた行は「正本非準拠データ」として扱う — その因子をカバレッジから
> 除外し、**`missing_map` に「必須列欠損（Contract 逸脱）」として明示計上して確度を下げる**（黙って通さない）。
> `deal_id` のみ行識別に不可欠なため、欠落行はスキップする。つまり「必須列も普通に欠損許容する」のではなく、
> 「正本準拠データでは揃う前提。逸脱データは落とさず検知して確度で正直に示す」という設計。

| 列名（正規化後） | 型 | 用途 | 必須（正本 1.0.0） | 欠損時の扱い（本 AG の欠損許容） |
|---|---|---|---|---|
| `deal_id` | str/int | 案件の一意キー（`missing_map` / `scored_deals` / `qualitative` のキー） | **必須（PK）** | 欠損行はスキップ（識別不能のため） |
| `deal_name` | str | 案件名（レポート表示・内部トレース用途。`deal_id` で代替可） | 任意（NULL 可） | 欠損時は `"(名称未設定)"` で表示（表示のみのため落とさない） |
| `account_name` | str | 顧客企業名（レポート表示・案件識別に使用） | 必須（NULL 不可） | 欠損時は `"(企業名未設定)"` で表示 |
| `phase` | str | 商談フェーズ。スコアの主因子。⚠️ **enum 値集合は正本 1.0.0 では未確定** — 本 AG は 1.0.0 の間は phase を**文字列として受け**、enum 検証は正本の 1.1.0 改版後に有効化する | 必須（NULL 不可） | 因子から除外・カバレッジ減（欠損は経路(b) の Contract 逸脱として `missing_map` 計上） |
| `amount` | number | 想定受注金額。スコアの主因子（円に正規化して寄与） | 任意（NULL 可） | 因子から除外・カバレッジ減 |
| `last_updated` | date/datetime | 最終更新日。StagnationDetect の停滞判定＋スコアの経過日数因子 | 必須（NULL 不可） | 停滞判定不可・因子から除外（欠損は経路(b) の Contract 逸脱として `missing_map` 計上） |
| `bant_budget` | bool/str | BANT: 予算確認状況 | 任意（NULL 可） | BANT 充足率の分母から除外 |
| `bant_authority` | bool/str | BANT: 決裁者接触状況 | 任意（NULL 可） | 同上 |
| `bant_need` | bool/str | BANT: ニーズ確認状況 | 任意（NULL 可） | 同上 |
| `bant_timeline` | bool/str | BANT: 導入時期 | 任意（NULL 可） | 同上 |
| `notes` | str | 案件備考・議事録の自由記述。QualitativeAnalysis（LLM）の定性リスク抽出の入力 | 任意（NULL 可） | LLM 分析スキップ（決定論スコアのみ） |
| `owner` | str | 営業担当（自社側）。`input_context` の**担当フィルタ**・レポート表示に使用 | 任意（NULL 可） | 列なし／欠損＝担当フィルタ自動無効・空欄表示（`column_map` に owner マッピングが無ければ機能オフ） |

> **正本との整合メモ（2026-07-03 更新）**: 本表は正本 `data_contract.md` **contract_version 1.0.0**（owner 追加後の
> commit `8d84fcc` 反映版）に準拠する。`owner` は要望が採用され、1.1.0 待ちでなく**未マージの 1.0.0 に
> 前倒し収載**された（任意・NULL 可・現時点で消費するのは本 AG のみ）。`deal_name` は正本で NULL 可（トレース用途・`deal_id`
> 代替可）。`phase` の enum 確定は正本 1.1.0 待ち（本 AG は文字列受けで先行実装可）。正本改版時に追従する。
> 本テンプレートが準拠する正本は、共有・版管理された **Enterprise Sales Support Data Foundation Builder Agent `deals` Data Contract / version 1.0.0** である。

> **列名マッピング**: 実テーブルの列名が上表と異なる場合、`config/config.yaml` の `column_map`
> （`{正規化名: 実列名}`）で吸収し、コード変更なしで合わせられる設計とする（経路(a)/(b) どちらの整備でも
> 同じ仕組みで適合させる）。これにより deals スキーマの最終確定が正本側で遅れても、本 AG の実装・
> テスト（fake DB）は先行できる。本仕様が準拠する Data Contract 正本は Enterprise Sales Support Data Foundation Builder Agent `docs/data_contract.md`
> （準拠 contract_version 1.0.0）。この共有契約は **Enterprise Sales Support Data Foundation Builder Agent `deals` Data Contract / version 1.0.0**
> として識別し、正本の改版時に追従する。
>
> **pre_process の処理順序（固定）**: (1) `DealRepository.list_deals()` で fetch →
> (2) `column_map` 適用（実列名→正規化名） → (3) S-2 `_extra_security_gate_input()` で PII スキャン・
> スコアリング不要な PII カラムをドロップ／マスク → (4) Contract 準拠検証（必須列 `deal_id` 存在・
> スコア寄与列の型検証） → (5) 検証結果から `deals`（通過行）・`missing_map`・`non_conformant` を構築。
> **この順序を固定する**（column_map は検証・PII 判定より前／PII ドロップは Contract 検証より前＝以降の検証・
> スコアは PII 除去後のデータに対して行う）。
>
> **経路(b) ドリフト防御（Contract 準拠検証）**: 上記 (4) で (i) 必須列 `deal_id` の存在、(ii) スコア寄与列の
> 型検証（`amount` 数値化可否・`last_updated` 日付パース可否・`bant_*` bool 解釈可否）を検証する。型違反の
> 列は欠損扱いで因子から除外して `missing_map` に理由つきで計上する。`deal_id` を持つ行のみ `deals` に残す
> （`deal_id` 欠落行はスキップ）。未知列は無視する（前方互換）。これにより Enterprise Sales Support Data Foundation Builder Agent の品質検証を経ない
> 経路(b) データでも、不正データがスコア・LLM 定性分析へ混入する前に検出・隔離される。
>
> **空 `deals` の分岐（State 表と route の整合）**: pre_process 後の `deals` が 0 件になるケースを (A)(B) と
> DB アクセス失敗 (C) の 3 つに区別する。**(A)(B) は `status="error"` にはしない**（route が finalize へ流し、
> 確認サマリを正常出力する）。(C) のみ error にする。
>
> ⚠️ **重要（route 契約との整合）**: `pre→main` は無条件辺のため、**main（GraphNode）は常に起動する**。
> 「スキップ」はしない。(A)(B) の 0 件時は、`extract_input` が空 `deals` を内側へ渡し、**内側先頭
> `StagnationDetectNode` が「対象 0 件」を検知して各サブノードを no-op 通過させ即戻る**（0 件を LLM に
> 投げない・スコアも空）。`merge_output` は空の `scored_deals` / `qualitative` を返し、post_process が
> `non_conformant` / 件数を見てサマリ文面を切り替える。この「main は走るが内側が 0 件で即戻る」動作を
> 前提に State 表の `deals`「0 件可」・`non_conformant` フラグを解釈すること（main を条件分岐で飛ばす
> 実装ではない＝`route()` / `add_edges()` は override しない原則を保つ）。
> - **(A) Contract 非準拠**（全行 `deal_id` 欠落 = スキーマがそもそも合っていない）: `non_conformant=True`・
>   `status="success"`。上記のとおり内側は 0 件即戻り。post_process が「**Contract 非準拠の可能性・
>   要スキーマ確認**」を明示した確認サマリ Markdown を生成する。
> - **(B) フィルタ結果 0 件**（スキーマは準拠だが `input_context` の絞り込みで対象案件が無い）:
>   `non_conformant=False`・`status="success"`。post_process が「該当案件なし」サマリを返す。
> - **(C) DB 接続・fetch 自体の失敗**（テーブル不在・認証失敗等）: これは上記と異なり **`status="error"`** を
>   立てる（route が finalize へ・main はパススルー）。「データが 0 件」と「DB にアクセスできない」を混同しない。
>
> **PII カラムの扱い（S-2 連携）**: `deals` が `owner` の email / 電話等の PII カラムを含む場合、
> pre_process はスコアリングに不要な PII を **State 格納前にドロップ／マスク**し、スコア因子と `notes` /
> 表示用 `deal_name` / `owner`（氏名のみ）だけを残す（本節の表に PII 連絡先列は含めない = 本 AG は要求しない）。
>
> **BANT 型の正規化**: `bant_*` は bool / "yes"/"no" / "確認済"/"未" 等の表記ゆれを pre_process で bool へ
> 正規化する（正規化ルールも config 化）。BANT 充足率 = 充足した BANT 項目数 / 取得できた BANT 項目数
> （欠損項目は分母に含めない = 欠損許容）。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [x] ConnectionPolicy (retry/timeout strategy)
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — domain-specific input check hook
      pre_process で `deals` を DB から取り込む際、`notes` に限らず **`deals` 行全体**（顧客担当者名・
      email・電話番号・BANT フィールド等）を対象に PII スキャンを実装する（レビュー指摘 #4 対応）。既定の
      `user_input` / `validated_input` スキャンは DB 由来フィールドを見ないため、ドメイン拡張フックで
      **State へ格納する前・かつ内側グラフ／LLM へ渡す前**に走らせる。方針は (a) スコアリングに不要な
      PII カラム（担当者 email・電話等）は pre_process の `project_kept_columns()` で **State 格納前に
      ドロップ**し、スコア因子（フェーズ・金額・BANT フラグ）と `notes` / 表示列のみを残す、(b) 残した
      各行の全文字列フィールドを S-2 フックで再スキャンし、検出した PII スパンを **その場でマスク
      （`[REDACTED]` 置換）** して `_pii_flag` を立てる。これにより LLM プロンプト／出力に生 PII が渡る前に
      露出面を最小化する。`_security_gate_input()` は override しない。
- [x] S-3: `_extra_security_gate_output()` — domain-specific output check hook
      post_process で Markdown レポートを出力する前に、(a) 既定 credential スキャンに加え、(b) LLM 所見
      （`qualitative`）に混入し得る PII の再チェックを行う。`_security_gate_output()` は override しない。
- [x] S-4: `emit_trace_event()` — at least one domain-specific event inside each `execute()`
      pre_process の DB fetch・QualitativeAnalysisNode の LLM 呼び出しなど、副作用を伴う操作ごとに
      `from shared.utils.audit_logger import emit_trace_event` を `execute()` 内で呼ぶ。
      `node_start` / `node_complete` / `node_error` は `BaseNode.__call__()` が自動発火するため重複させない。

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass → framework `@final` gate always runs automatically;
>   extend via `_extra_security_gate_input()` / `_extra_security_gate_output()` only
> - `GraphNode` / `RemoteAgentNode` → deliberate no-op (upstream or remote node's gate already applied)
> - Custom `BaseNode` subclass → must implement `_security_gate_input()` and
>   `_security_gate_output()` directly (`@abstractmethod` — omission raises `TypeError` at instantiation)

本テンプレでは pre_process / post_process を `FunctionNode` サブクラスとし、拡張は `_extra_*` フックで行う。
main は `GraphNode` のため S-2/S-3 は deliberate no-op（内側サブノードの gate と post_process の S-3 で担保）。

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — main スロットに `DomainWorkflowGraphNode` を配置し、内側の
  `domain_workflow_graph`（4 サブノードの決定論＋LLM パイプライン）を wrap する
- **Composition target**: `src/graph/domain_workflow_graph.py`（内側 `AgentBaseGraph` / `BaseGraph`）
- **Error propagation strategy**: propagate — 内側サブノードで発生した error は `status="error"` として
  外側へ伝播し、main → post_process はパススルー、finalize で error 応答を返す。DB fetch 失敗・LLM 失敗は
  握りつぶさず伝播する（沈黙して誤らない設計）

> **Cat2 GraphNode subgraph 契約（SDK 1.0.0 実機・データ受け渡しの具体）**: 内側グラフは `user_input`(str) +
> ctx のみを受領し、外側の任意 State キー（`deals` / `missing_map` / `input_context`）は自動伝播しない。
> このため本テンプレは以下の明示的な直列化／逆直列化の契約で構造化データを橋渡しする（レビュー指摘 #3 対応）:
>
> 1. **外側→内側（送り）**: `DomainWorkflowGraphNode.extract_input(state)` を override し、pre_process が
>    構築した `deals` / `missing_map` / `input_context` を **1 つの JSON 文字列**にまとめて返す
>    （`json.dumps({"deals": ..., "missing_map": ..., "input_context": ...}, default=str, ensure_ascii=False)`）。
>    これが内側グラフの `user_input`(str) になる（GraphNode は単一 `user_input` 文字列しか内側へ渡せないため、
>    複数構造体は 1 つの JSON にパックする）。
> 2. **内側（先頭で復元）**: 内側グラフの先頭 `StagnationDetectNode.execute()` で `json.loads(state["user_input"])`
>    し、`deals` / `missing_map` / `input_context` を内側 State のキーへ展開する（以降の Scoring /
>    PriorityDecision / QualitativeAnalysis は展開後のキーを直接参照）。
> 3. **内側→外側（戻し）**: `DomainWorkflowGraphNode.merge_output(state, sub_result)` を override し
>    （**SDK 1.0.0 実機の abstract メソッドは `merge_output(self, state, sub_result: dict) -> dict`**。
>    `assemble_output` という名称は存在しない）、内側が算出した `scored_deals` / `qualitative` を外側 State
>    へマージした dict を返す（変更キーのみ返す。full state 返却は reducer で二重化するため不可）。
>
> `default=str` は `datetime`（`last_updated` 等）を安全に文字列化するため必須。JSON パック方式のため、
> `deals` は primitive のみ（flat・msgpack 互換）である前提を pre_process 側で保証する。checkpointer は
> HITL 無しのため標準構成（`GraphNode._parent_config` の memory_enabled 伝播は不要）。

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

Level 0（`agenticstar` SDK）の直接 import は行わない。import は `framework.*`（AgentBaseGraph / GraphNode /
BaseNode / AgentState / InvocationContext / TrustLevel / SecurityViolationError）と `shared.*`
（`shared.services.database` / `shared.utils.audit_logger`）に限定する。

### 永続化の差し替え境界

`src/services/deal_repository_factory.py` の `create_deal_repository()` を backend の組み立て境界とする。
pre_process は database adapter や SQL を直接扱わず、factory が返す `DealRepository` の公開操作だけを使う。
将来、実装済み PostgreSQL / MySQL backend へ移行する場合も、この factory で repository 実装を選択し、
ノードや retrieval 処理だけを差し替えない。これにより読み取りと登録処理が同じ store を参照する。

backend の受け入れ契約は `tests/contract/test_deal_repository_contract.py` に集約する。契約が固定するのは、
案件の登録・ID 取得・owner/phase の完全一致フィルタ・ファイル再オープン後の永続性である。テストは repository
公開 API のみを呼び、SQL 文、`LIKE` の挙動、語分割、SQLite 固有 API を仕様に含めない。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph (L1 Base)** | 固定パイプライン型のマルチステップであり自律ループ不要。停止条件はパイプライン完了で決まる |
| Composition pattern | Standalone (3 slots) | GraphNode (subgraph) | **GraphNode (subgraph)** | 決定論 3 ノード + LLM 1 ノードのドメインワークフローを内側グラフに encapsulate し、外側 backbone を単純に保つ |
| データ取り込みの責務 | 本 AG が非定型ファイルを取り込む | 外部整備の Contract 準拠データを参照のみ | **外部整備・参照のみ** | 単一責任。整備側は同じ SQLite ファイルへ repository 契約で登録し、本 AG の invoke は参照だけを行う |
| 定性抽出の対象 | 全案件を LLM に投入 | 備考あり/上位 N 件に絞る | **絞り込み** | 大量行のトークン超過とコストを回避。決定論スコアは全件、LLM は備考が埋まった案件に限定 |
| スコアと所見の提示 | 統合して 1 スコアに丸める | 決定論スコアと LLM 所見を分離 | **分離** | 説明可能な決定論スコアと不確実な LLM 所見を混ぜず、過信を防ぐ compliance-by-construction |

## 既知の前提・限界（レビュー時の論点・押印前確認）

> 押印前の差分レビュー（2026-07-01）で挙がった論点を、隠さず設計文書に明記する。

- **依存の実体は「Data Contract 準拠の `deals` データの存在」であり、Enterprise Sales Support Data Foundation Builder Agent という AG ではない
  （レビュー指摘 #5・#1 への回答を 2026-07-03 クラスタ方針で更新）**:
  本 AG は SQLite の `deals` テーブルを**読み取り参照**する。SDK 1.0.x の PostgreSQL / MySQL client は
  `BaseDatabase` の STUB であるため採用せず、リポジトリ内の SQLite 実装を repository factory から構築する。
  **本 AG の実装・単体/PB テスト（repository/fake DB 注入）は基盤整備と並行して進められる**。
  一方、**STG での実 invoke 動作確認・リリースには Contract 準拠の `deals` テーブルが実データで整備済み
  であることが前提**となる（整備経路は経路(a) Enterprise Sales Support Data Foundation Builder Agent / 経路(b) 顧客直接用意のいずれでも可 —
  リリースゲートで確認するのは**データの存在と Contract 準拠**であって Enterprise Sales Support Data Foundation Builder Agent の導入ではない）。
  したがって**本 AG は Enterprise Sales Support Data Foundation Builder Agent 無しでも単体導入できる**（SFA/CRM 導入済み顧客は経路(b) で
  エクスポートを直接用意すればよい）。SFA 非導入層に元データが無いという指摘に対しては、経路(a) で
  **基盤 AG Enterprise Sales Support Data Foundation Builder Agent がテナントの非定型ファイル（Excel/CSV/議事録）を `deals` テーブルへ取り込む**
  ことで対応する（この層に対してはクラスタとしての価値）。なお経路(b) のデータには Enterprise Sales Support Data Foundation Builder Agent の
  品質検証（欠損/重複/型不整合・確度ゲート）が掛からないため、品質担保は顧客責務（本 AG 側の欠損許容
  原則・カバレッジ明示・「要手動確認」フラグが防御線）。
- **Cat 2 判定の根拠（レビュー指摘: Cat1 寄りでは、への回答）**: 本 AG は自律ループを持たない固定パイプラインだが、
  Cat 2 の定義は「複数ステップを束ねてユースケースを完遂」であり自律ループは Cat 3 の要件。DB 取得（欠損許容）
  → 決定論スコア（カバレッジ明示）→ ルール優先度 → LLM 定性抽出 → スコア/所見分離の確度明示、という
  ドメイン知識を埋め込んだマルチステップを 1 デプロイ単位で束ねる点が Cat 2。単一の汎用能力（Cat 1）でも
  純粋関数（Tool）でもない。この分類はレビューを経て確定している。
- **PII 露出面（レビュー指摘 #4 対応済み）**: S-2 で `deals` 行全体を対象にし、スコアリング不要な PII カラムは
  State 格納前にドロップ／マスク。LLM へは `notes` とスコア因子のみを渡す。S-3 で出力の再チェック。
- **決定論スコアの重み付けは config 化**: フェーズ×金額×経過日数×BANT の重み・停滞閾値・routing_table は
  `config/config.yaml` に外出しし、組織ごとにチューニング可能とする（スコア基準の標準化・監査可能性）。

## AGENTIC STAR Marketplace adapter

リポジトリ直下の `cli.py` は Marketplace の単一文字列入力を既存グラフへ接続し、JSON オブジェクト内の
`owner` / `phase` だけを公開フィルタとして再構築する。会話履歴・`today`・`_config` は渡さず、LLM は
呼び出し時に allowlist 済み環境変数から接続値を解決する Azure seam へ差し替える。
