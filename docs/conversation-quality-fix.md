# Latest conversation regression fix

The reported question was `哪些年龄组更常因费用而延迟或未获得医疗服务？`. Reading its saved conversation exposed three separate defects:

- The router identified both cost-barrier outcomes, but the intent validator recognized only unmet care. It rejected the delayed-care member before R ran. A generic answer then mislabeled that policy rejection as failed statistical checks.
- Questions about the dataset's record count/year were sent to the current-result explanation branch, which repeated the last age-group table.
- Ordinary chat accepted a Chinese-language request while the statistical evidence editor required English. The language preference was not persisted, and language-only follow-ups reused the wrong topic.

The shared `outcome_intent.py` resolver now supplies the same original-text spans and outcome IDs to routing and validation. Shared-object phrases in Chinese and English retain both events. The execution consistency guard remains active and rejects substituted outcomes or groupings. The English causal guard also matches `cause` as a complete word, avoiding false rejection of `because of cost`.

Dataset questions have a separate intent and response source. Clear requests read verified `data.status` metadata directly; the API planner also supports a bounded `dataset` intent for paraphrases. Generated counts are discarded. Whole-file adult records, a particular analysis's valid denominator, income imputation rows and download time are kept distinct. Metadata answers carry a dataset source, not the previous run ID or chart.

English remains the default interface/reply language until an explicit preference. Chinese/English preferences persist in conversation context, including recovery from instructions in older saved chats. A language-only request resolves the preceding substantive question and reuses its corresponding evidence without rerunning statistics. Chinese result text is rendered from the same validated numerical fields. The API editor can reference immutable Chinese evidence blocks but cannot translate/rewrite numbers or add unchecked prose. Locale is included in the evidence-integrity hash.

The paired age comparison now leads with each measure's observed highest group and uses one table. Detailed explanations remain available on request; plural references retain both source runs. Chinese sample-size, confidence-interval and method follow-ups answer that topic rather than repeating the entire table. Partial failures retain their actual policy/statistical reason and do not substitute self-rated health.

## Evidence

- Full Python suite: 559 tests run, 558 passed, one Linux-only resource check skipped on macOS.
- Focused conversation tests reproduce the medical question → dataset question → Chinese rewrite → restart/reopen → dataset question → paired research → detailed explanation → English switch sequence.
- Locale tests retain evidence order, all numerical text and limitations, and reject locale tampering or added Chinese model claims.
- A controlled run used the actual verified NHIS inputs in a temporary conversation/workspace. Both outcomes published four age groups without R warnings. Model transport was explicitly disabled for this numerical/formatting check; this is not a new live-provider certification.
- Original conversations and runs were inspected but not rewritten or deleted. No environment file was read or changed.

Observed weighted estimates and 95% CIs, within the registered U.S. adult age domain:

| Age group | Delayed care due to cost | Unmet care due to cost |
|---|---:|---:|
| 25–34 | 12.45% (11.26%–13.64%) | 10.41% (9.31%–11.52%) |
| 35–44 | 10.36% (9.33%–11.39%) | 9.64% (8.67%–10.62%) |
| 45–54 | 10.81% (9.74%–11.89%) | 9.58% (8.57%–10.60%) |
| 55–64 | 9.28% (8.33%–10.24%) | 7.79% (6.94%–8.65%) |

Valid outcome denominators are 19,478 and 19,475 respectively. These are separate overlapping events, not a mutually exclusive distribution. This point-estimate ranking is not a formal test of between-group differences. The underlying adult file has 32,629 records from the 2024 survey; its income file has 326,290 rows across ten imputations, not additional unique respondents. Independent Stata/external benchmarking remains pending.

The private local preview and structured validation receipt are under `.cache/previews/age-care-fixed-*` and are excluded from Git. The current execution environment cannot bind a listening port (`EPERM`), so it cannot replace the already-running local server. Restart the original startup terminal to load the changed backend:

```bash
bash /Users/jiahui.zhu/nhis-health-agent/scripts/start.command
```

Stop the previous process with Control-C in that terminal first. Resend the question after restarting; old saved assistant messages remain historical records.
