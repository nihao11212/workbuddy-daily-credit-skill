---
name: workbuddy-daily-credit
display_name: WorkBuddy 每日签到
display_name_en: WorkBuddy Daily Check-in
description: Check and claim today's Buddy Station daily sign-in credits for the current WorkBuddy account on Windows.
description_zh: 在 Windows 本机检查并领取当前登录 WorkBuddy 账号的 Buddy 加油站每日积分。
description_en: Check and claim the current WorkBuddy account's Buddy Station daily credits on Windows.
version: 1.0.0
author: WorkBuddy community contributor
---

# WorkBuddy 每日签到

仅在用户明确要求领取，或用户已创建的定时任务明确调用本技能时执行。

## 执行步骤

1. 确认当前系统为 Windows，且本机 WorkBuddy 桌面端已安装并登录。需要 Python 3；优先使用 WorkBuddy 提供的 Python 运行时，否则使用本机 `python` 或 `py -3`。脚本只依赖 Python 标准库。
2. 先解析已安装技能的根目录，并从该目录运行脚本（不要假设当前工作目录就是技能目录）。用户只想查看状态时运行：

   ```powershell
   python <skill-root>/scripts/checkin.py --status
   ```

3. 用户要求领取时运行：

   ```powershell
   python <skill-root>/scripts/checkin.py
   ```

4. 根据脚本 JSON 输出确认 `result`、`today_checked_in`、`today_credit`、`streak_days`。`CLAIMED` 必须同时满足 `today_checked_in: true`；`ALREADY_CLAIMED` 表示当天已领。不要仅凭退出码判断成功。
5. 仅在用户要求时汇报。遇到登录失效、活动结束、接口错误或无法复核时，如实说明并停止；不得不断重试。

脚本先查询当天状态，只有活动有效且当天未领取才领取；领取后会再次查询确认。它只使用 Buddy 加油站每日签到，不执行旅行、盲盒、成长中心或其他奖励。

## 安全边界

- 仅从本机 WorkBuddy 登录态读取当前账号凭据，并在内存中解密使用。不得在回复、日志、文件、命令参数或产物中输出令牌、UID、原始凭据或解密密钥。
- 只向固定 HTTPS 主机 `copilot.tencent.com` 发送签到请求，禁止重定向和其他目标地址。
- 不安装依赖、不启动其他程序、不读取或上传其他用户文件。不要要求用户把登录态文件发给你。
- 登录态路径为 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`。凭据不会放入技能包。
- 脚本只在 `%LOCALAPPDATA%\WorkBuddyDailyCredit\results.jsonl` 写入不含凭据的签到状态摘要。
- 接口为客户端内部、未公开文档的接口，可能随 WorkBuddy 更新而变化。遇到格式变化时停止并报告，不猜测新接口。

## 创建每日定时任务

用户希望自动签到时，指导其创建 WorkBuddy **本地**定时任务，每天在用户指定的当地时间调用本技能，例如提示词“使用 WorkBuddy 每日签到技能领取今天的 Buddy 加油站积分，并按技能规则核验”。先让用户手动运行一次确认可用。不要擅自选择账号、启用其他奖励或推送渠道。

