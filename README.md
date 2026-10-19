# WorkBuddy 每日签到 Skill

这个 Windows Skill 只检查并领取当前登录 WorkBuddy 账号的 Buddy 加油站每日登录积分。它先查询当天状态，只有活动有效且尚未领取时才请求领取，并在领取后复查状态。

## 安装

公开仓库：<https://github.com/nihao11212/workbuddy-daily-credit-skill>

直接下载：<https://github.com/nihao11212/workbuddy-daily-credit-skill/archive/refs/heads/main.zip>

从 GitHub 仓库点击「Code → Download ZIP」获取技能包，或使用单独分发的 `workbuddy-daily-credit.zip`。然后在 WorkBuddy 中打开「专家·技能·连接器 → 技能 → 添加技能 → 上传技能」，选择 ZIP。其他电脑需分别导入此 ZIP，并安装、登录 WorkBuddy 桌面端。

## 使用

用户明确要求领取时，WorkBuddy 调用技能脚本。状态查询使用 `python scripts/checkin.py --status`；领取使用 `python scripts/checkin.py`。脚本只用 Python 标准库。

如要每天自动运行，请在 WorkBuddy 中创建一个本地定时任务，提示词为“使用 WorkBuddy 每日签到技能领取今天的 Buddy 加油站积分，并按技能规则核验”。选择你需要的本地时间。WorkBuddy 本地定时任务执行时电脑和客户端需要可用；每次运行会调用 Agent，可能消耗 WorkBuddy 用量。

## 前置条件

- Windows 10 或 Windows 11。
- WorkBuddy 桌面端已安装并登录。
- Python 3 可由 WorkBuddy 运行时或系统 `python` / `py -3` 命令调用。

若脚本无法找到 Python 或 WorkBuddy 的运行时，请停止并报告错误；不要改用未经审阅的下载脚本。

## 凭据和隐私

Skill 从 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` 读取本机 WorkBuddy 登录状态，使用 WorkBuddy 桌面程序在内存中解密。它不会把令牌、UID 或原始凭据写入 Skill 包、日志或输出。签到请求只发往 `https://copilot.tencent.com`，并拒绝 HTTP 重定向。状态摘要保存在 `%LOCALAPPDATA%\WorkBuddyDailyCredit\results.jsonl`。

这两个签到接口属于客户端内部、未公开文档的接口，可能随 WorkBuddy 更新而变化。Skill 与 WorkBuddy / 腾讯无官方关联；活动或接口变化时签到可能失败。建议先阅读 `SKILL.md` 与脚本再安装。

## 开源许可

`scripts/auth_helper.py` 的加密凭据处理代码摘自 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin) 的 `signin.py` 前 527 行，依 MIT 许可证分发；见 `LICENSE.auth-helper`。本包不包含其成长中心、补签、旅行、任务奖励等功能。

签到接口参考 [SIMON-WORLD/workbuddy-daily-credit](https://github.com/SIMON-WORLD/workbuddy-daily-credit)。

