---
name: dev-cycle
description: 依序執行 planner → coder → tester → reviewer 完成一個功能開發。當使用者要求開發新功能、實作需求時使用。
disable-model-invocation: true
argument-hint: [功能描述]
---

針對以下需求，依序呼叫四個 subagent 完成完整開發流程，不要跳過任何步驟：

需求：$ARGUMENTS

1. 先用 **planner** subagent 針對這個需求規劃實作步驟
2. 把 planner 的計畫交給 **coder** subagent 實作
3. coder 完成後，用 **tester** subagent 撰寫並執行測試，只回報失敗案例
4. 測試通過後，用 **reviewer** subagent 做最終 review，輸出 Critical/Warning/Suggestion 分級結果

每一步驟結束後，先給我一句話摘要目前進度，再進行下一步。如果 tester 或 reviewer 發現問題，退回給 coder 修正，修正後重跑該步驟，不要整個流程從頭來過。
