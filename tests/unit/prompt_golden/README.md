# Prompt golden corpus

**用途**：`DefaultPromptContextBuilder.build()` 現行輸出的 byte-identical 快照，凍結於任何重構之前，當 DH4b（kwargs → `PromptSectionContext` → section registry）與 DH5（順序重排）的機械化 oracle。重構後每個 snapshot 必須逐 byte 相等；紅了預設是「重構改到輸出」，不是「快照過期」。

**矩陣**（`cases.py` 的 `GOLDEN_CASES`，19 組；`BRANCH_PAIRS` 列出每個互斥決策，結構測試強制兩側都有 fixture）：`minimal` / `full_material` / `material_digest_hit`（digest 命中清五塊素材）/ `today_scene_active` ↔ `story_scene_active`（exactly-one）/ `tools_and_outcomes` / `nsfw_frontier_sanitized` ↔ `nsfw_community_retained` / `experiment_overlay_off` ↔ `subjective_time_catchup`（overlay 清 self_reflection＋body_state＋主觀時間）/ `stage_nudge_silent`（省略最新訊息行）/ `stage_nudge_messaging`（texting-style）/ `retry_directive` / `vision_markers_and_recognition` / `address_change_lines` / `operator_persona_five_layers` / `older_dialogue_summary` / `history_gap_markers` / `schedule_and_world`。

**確定性**：所有輸入都是字面值——固定 id（`*-golden-*`）、固定 `now`（`factories.NOW`）、固定文字；沒有 `uuid4`、沒有 `datetime.now`。渲染時 `harness.pinned_prompt_environment()` 會清掉 `YURALUME_PROMPT_PACK_DIR` / `PROMPTS_DIR` / `KOKORO_PROMPTS_DIR`，一律對 repo 內 baseline pack 出圖。

**合法重生**：只有在「刻意且已審查的 prompt 變更」（新措辭／新區塊／DH5 重排）或 `src/kokoro_link/data/prompts/` 的 baseline pack 變更時才重生，且要**單獨一個 commit**、diff 只含預期的改動：

```
ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py          # 寫入
ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py --check  # 只報告漂移
```

沒設環境變數就直接退出（exit 2）——鏡像 baseline prompt guard 的 `ALLOW_BASELINE_PROMPT_UPDATE` 慣例，避免一鍵刷掉退化的證據。跑測試：`python -m pytest tests/unit/prompt_golden`。
