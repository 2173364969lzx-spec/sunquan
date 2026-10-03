# 孙权：学习记录接续

这是本地学习搭子项目。运行与记忆在本机，模型使用本地 Ollama。

- 进入或恢复学习任务时先调用 `study_snapshot`；来源不足时用 `study_search` / `study_read` 核对，不把缓存当作最新状态。
- 有明确新进度时用 `study_commit` 按次交回，携带当前 revision 和本次唯一 idempotency_key。区分已讲、本人掌握、提交与用时，不补造未确认的事实。
- 现有学习目录为原始来源，不删除、覆盖或复制出相互竞争的接续记录。新记录存放 data/learning.sqlite3。
- 已接入资料不等于全部 ChatGPT/Codex 历史。查看 data/coverage.json 了解范围与缺口。
- 不自动调用付费 API，不把本地资料发送给云端模型。联网查询仅在用户勾选后发送当前问题或所给网页链接。
- 修改后使用 `python -X utf8 -m unittest discover -s tests`，并对涉及界面或模型的变更做真实验收。
