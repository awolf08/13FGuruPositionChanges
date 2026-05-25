# 13F Guru Tracker

自动抓取一组知名投资人的最新 13F，和上一季度对比，输出仓位变化报告。

## Usage

SEC 要求自动访问带上能联系到你的 User-Agent。建议先设置：

```bash
export SEC_USER_AGENT="your-name your-email@example.com"
```

运行：

```bash
python3 guru_13f_tracker.py --investors investors.json --out reports
```

默认会缓存 SEC filing XML，但 SEC submissions 索引只缓存 24 小时，避免季度自动任务一直复用旧 filing list。需要强制刷新 filing list 时：

```bash
python3 guru_13f_tracker.py --refresh-submissions
```

如果用于自动化，希望任一投资人失败就返回非 0 exit code：

```bash
python3 guru_13f_tracker.py --strict
```

或者直接双击/运行：

```bash
./run_report.command
```

输出文件：

- `reports/latest_changes.md`: 汇总 Markdown 报告
- `reports/latest_changes.csv`: 所有变化明细
- `reports/latest_chart.html`: 彩色仓位变化图表
- `reports/raw/`: SEC 原始 JSON/XML 缓存

## Add Investors

编辑 `investors.json`，添加：

```json
{
  "name": "Investor Name",
  "manager": "SEC manager name",
  "cik": "0000000000"
}
```

## Quarterly Automation

macOS/Linux 可以用 cron 每季度跑一次，例如每年 2/5/8/11 月 16 日早上 8 点：

```cron
0 8 16 2,5,8,11 * cd /Users/weicheng/Desktop/Projects/13FGuruPositionChanges && SEC_USER_AGENT="your-name your-email@example.com" python3 guru_13f_tracker.py --investors investors.json --out reports --strict
```

13F 通常在季度结束后 45 天内披露，所以 2/5/8/11 月中旬运行比较合适。
