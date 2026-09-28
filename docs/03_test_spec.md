# Test Specification — CMN-C2-259

Enterprise Deal Scoring & Pipeline Priority Agent (Cat 2) のテスト仕様。
Source of Truth = (internal issue reference removed) / docs/02_design.md。

## テスト実行

```bash
source .venv/bin/activate
python -m pytest tests/ -v
python -m pytest tests/proof_of_boundary/ -v
```

すべての fake（repository / LLM）は**ファクトリ関数の monkeypatch** で注入する
（`src.services.deal_repository_factory.create_deal_repository` / `src.services.llm_client.build_llm`）。
ノードは `compile()` 時に再生成されるためインスタンス属性注入は使わない。

## TC（フレームワーク準拠・機能）

| TC-ID | 対象 | テスト内容 | 期待結果 | テスト |
|---|---|---|---|---|
| TC-01 | State 契約 | `State` は flat TypedDict（Pydantic/dataclass 無し） | PB スキャン合格 | `proof_of_boundary/test_state_safety.py` |
| TC-02 | 決定論スコア | 因子欠損時にその因子を除外しカバレッジ算出 | 欠損許容・coverage 正 | `unit/test_scoring_service.py::test_score_deal_missing_tolerant_coverage` |
| TC-03 | BANT 充足率 | 欠損項目を分母から除外 | present=2 で ratio=0.5 | `unit/test_scoring_service.py::test_bant_fulfillment_excludes_missing` |
| TC-04 | 優先度ルール | routing_table のトップダウン一致 | 期待 priority | `unit/test_scoring_service.py::test_decide_priority` |
| TC-05 | 停滞検出 | last_updated からの経過日数で判定 | 14 日超で stagnant | `unit/test_scoring_service.py::test_is_stagnant` |
| TC-06 | Contract 検証 | deal_id 欠落・型不正の検出 | 逸脱を missing_map 計上 | `unit/test_scoring_service.py::test_validate_row_flags_missing_and_types` |
| TC-07 | PII カラム除去 | column_map 適用後に PII 連絡先列をドロップ | kept に PII 列なし | `unit/test_scoring_service.py::test_apply_column_map_and_projection` |
| TC-08 | `required_trust_level` declared and enforced | Insufficient trust level → execution refused *before* `execute()`; test proves `execute()` was not called | `proof_of_boundary/test_pb_invoke_order.py::test_s1_trust_gate_refuses_insufficient_trust` + `unit/test_pre_post_nodes.py::test_s1_trust_gate_denies_anonymous_caller` |
| TC-09 | 空 deals (A) | 全行 deal_id 欠落 → 非準拠サマリ | status=success・非準拠文面 | `unit/test_pre_post_nodes.py::test_pre_process_non_conformant_all_missing_id` |
| TC-10 | 空 deals (C) | DB 接続失敗 → error | status=error・error_log | `unit/test_pre_post_nodes.py::test_pre_process_db_failure_is_error` |
| TC-11 | S-4 監査 | 各 execute で emit_trace_event を呼ぶ（副作用操作） | ドメインイベント発火 | 実装（各ノード execute 内） |
| TC-12 | エンドツーエンド | fake DB/LLM で 5 ノード backbone を invoke | success・Markdown 出力 | `integration/test_end_to_end.py` |
| TC-13 | S-1 トラストゲート | 全ノード `required_trust_level=VERIFIED_EXTERNAL`・ANONYMOUS 拒否 | status=error・"S-1 trust gate denied" | `unit/test_pre_post_nodes.py::test_s1_trust_gate_denies_anonymous_caller` |
| TC-14 | 永続化 repository 契約 | 登録/取得、owner・phase 完全一致フィルタ、再オープン後の永続性 | backend 固有 SQL・LIKE・語分割に依存せず全 backend で同じ結果 | `contract/test_deal_repository_contract.py` |

## PB（Proof-of-Boundary）

| PB-ID | 境界 | テスト内容 | 期待結果 | テスト |
|---|---|---|---|---|
| PB-2/5 | State 直列化 | 生 State に Pydantic/credential 無し | スキャン合格 | `proof_of_boundary/test_state_safety.py` |
| PB-4 | Import 分離 | src/ が Level 0（agenticstar）を import しない | AST スキャン 0 件 | `proof_of_boundary/test_import_isolation.py` |
| PB-6 | invoke 実行順序 | 5 ノード backbone が initialize→pre→main→post→finalize の順で走る | node_history に順序 | `proof_of_boundary/test_graphnode_contract.py::test_full_invoke_backbone_order` |
| PB-C2a | GraphNode extract_input | deals/missing_map/input_context を 1 JSON 文字列にパック | 復元して一致 | `proof_of_boundary/test_graphnode_contract.py::test_extract_input_packs_json` |
| PB-C2b | GraphNode merge_output | 変更キーのみ返す（full state 禁止） | 3 キーのみ・node_history 無し | `proof_of_boundary/test_graphnode_contract.py::test_merge_output_returns_only_changed_keys` |
| PB-C2c | datetime 直列化 | extract_input が date を default=str で安全化 | 例外なし・ISO 文字列 | `proof_of_boundary/test_graphnode_contract.py::test_extract_input_handles_datetime_via_default_str` |

## 実 LLM 1 回転チェック（Live LLM Verification）

`config/agent.yaml` の `generation_mode: "llm"` 宣言に対する実測記録。上記 TC/PB は
すべて fake LLM（`.complete()` が整形済み JSON を即返す）を注入しているため、
「プロンプトが出力契約を実際に導けるか」は検証できない。ここでは実 LLM を注入シームへ
刺して 1 回だけ invoke し、使える出力が出ることを確かめた。

| 項目 | 実測値 |
|------|--------|
| 実施日 | 2026-08-20 |
| 使用 LLM | ローカル CLI（Azure OpenAI）— 検証専用の `BaseLLM` 実装 |
| 注入シーム | `QualitativeAnalysisNode._get_llm()`（`tests/conftest.py::agent_config` が live-check 用に適合。下記「シームの注意」参照） |
| 入力 | `tests/conftest.py` の `case_payload="run"` / `live_input_context={"today": "2026-07-03"}` / `agent_config`（統合テスト `test_end_to_end.py` と同一の CONFIG・DEALS 2 件） |
| 実 LLM 呼び出し回数 | **1 回**（`notes` を持つ案件のみが対象・`max_deals=20` で 1 バッチにまとめて送信。D1 のみ該当、D2 は `notes` 空で対象外） |
| 結果 | ✅ `status: success` |
| 非空だった出力キー | `output`（Markdown レポート 491 字・`result` / `formatted_output` と同一本文） |
| 縮退 | なし |

実行手段: 手元で実 LLM を 1 回転させる手動検証。CI には入れない（API キーを要するため）。

**実 LLM でのみ確認できたこと**（fake では出ない性質）:

- `_SYSTEM_PROMPT` が出力契約 `{"items":[{"deal_id","risks","actions"}]}` を実 LLM に
  正しく伝えられている。素の JSON が返り、`_parse()` が復元に成功した
  （`_parse()` は失敗時に `({}, False)` を返すため、下記のとおりレポートに LLM 所見が
  載っていること自体が復元成功の証拠になる）。
- **LLM 所見が実際にレポートへ到達している。** これが本 AG で最も重要な確認点で、
  `_parse()` が失敗しても `qualitative={}` のまま `status=success` で返る設計のため、
  決定論スコアだけで出力が非空になり「LLM の寄与が黙って消えても ✅ に見える」危険がある。
  今回は D1 行の `定性リスク(LLM)` / `推奨アクション(LLM)` 列が実 LLM の生成内容
  （決裁者の慎重姿勢・競合B社の価格提示リスク、および対応アクション 3 点）で埋まっており、
  空集合を通した PASS ではないことを確認した。
- `notes` が空の D2 は LLM に送られず、レポート上も `—` 表示。「notes のある案件だけを
  LLM に送る」という設計どおりに絞り込みが効いている。
- 決定論スコア（D1: 0.73 / P2-high、D2: 0.26 / P4-low）と LLM 定性所見が別列に分離され、
  「決定論スコアは説明可能・LLM 所見は要確認」という分離提示が実出力でも保たれている。

**シームの注意（`src/` は未変更）**

本 AG は LLM を *クライアント実体* ではなく **設定 dict** として運ぶ。設定は
`DealScoringGraphNode.extract_input()` で JSON 文字列に直列化されて内側グラフへ渡り
（`input_context["_config"]["llm"]`）、`QualitativeAnalysisNode._get_llm()` が
`llm_client.build_llm()` で実体を組み立てる。そのため一般的な `config["llm"]` へ
`BaseLLM` オブジェクトを差す注入は、JSON 境界で文字列化されて成立しない。
`tests/conftest.py::agent_config` はこの live-check シームだけを適合させるもので、
`src/` には一切手を入れていない。`execute()` 本体（プロンプト組み立て →
`llm.complete()` → `_parse()`）は無改変のまま走っており、検証対象は損なわれていない。

なお live-check 経路は統合テストと同様に `api_key_secret` を経由しないため、
`_get_llm()` 内の `current_secrets().require()`（本番の secret 解決）は今回の
1 回転では実行されていない。secret 解決の検証は TC 側の責務。

## カバレッジ方針

- 決定論ロジック（`src/services/service.py`）は純粋関数として網羅的に単体テスト。
- ノードは execute 単位で正常系・境界（欠損・非準拠・DB 失敗）を検証。
- Cat2 の外側/内側境界（GraphNode 契約）は PB で明示検証。
- LLM は fake（BaseLLM 互換 `.complete()`）を注入し、外部 API を呼ばずに検証。
