---
name: discover-kol
description: 发现新票——在 X 上看 invest KOL 名单最近提到哪些股票，整理成候选，经用户点名后存进 lynch 候选区。触发：发现新票、看看 KOL 最近提到哪些票、拉 KOL 候选、discover kol。需要 X 搜索工具（grok）。
---

# 发现新票（discover-kol）

搜索规则与候选块格式的唯一来源是 `references/search.md`；lynch 手机 app 的「发现新票」对话每轮把它整段发给 Grok。本文件是在 lynch 目录里开终端会话时的流程。

## 流程

1. 在 lynch 根目录跑 `just kol-sources`，拿到 X 列表、账号、回看时长和起始日期。失败就停下，把报错原样告诉用户（名单在 `../invest/config/kol_handles.toml`，lynch 不改它）。
2. 读 `references/search.md`，按它搜索、整理，回答末尾附候选块。
3. 问用户要把哪几只存进候选区。用户没有明确点名，不落盘。
4. 把整段回答（含候选块）写到 `_local/tmp/discover-<YYYYMMDD-HHMM>.md`，然后跑：

   ```bash
   just candidates-save _local/tmp/discover-<YYYYMMDD-HHMM>.md <TICKER> [<TICKER> …]
   ```

   脚本只存候选块里有的票；有点名的票不在块里时整次不写。把脚本输出原样告诉用户。

## 不要做

- 不写 `../invest` 的任何文件，不改 KOL 名单。
- 不手写或手改 `output/candidates/candidates.json`，只经 `just candidates-save`。
- 不在 `frameworks/` 里写任何东西；要研究某只票，回到 lynch 的新票调研流程。
