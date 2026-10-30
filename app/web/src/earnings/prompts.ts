import { hitWhen, moveText } from '../anomalies/model';
import type { AnomalyItem } from '../anomalies/types';
import { SLOT_CN } from './model';
import type { WatchItem } from '../watch/types';
import type { EarningsItem } from './types';

/** Research replies run long on a phone: every prompt asks for the same short summary up front. */
function tldr(gist: string, details: string): string {
  return `回复开头先写 TL;DR（不超过 5 行）：${gist}；${details}放在 TL;DR 后面。`;
}

/** The built-in research prompt, sent as the chat's first message so it stays visible in the conversation. */
export function earningsPrompt(it: EarningsItem): string {
  // "2026-10-14 盘后" with a known slot, "2026-10-14 " without: the template follows with 发布.
  const when = SLOT_CN[it.slot] ? `${it.date} ${SLOT_CN[it.slot]}` : `${it.date} `;
  const fw = `frameworks/${it.ticker}.md`;
  // An anomaly filed under this release rides along so the move is explained with the report.
  const filed = it.anomaly
    ? [`财报前后出现异动：${hitWhen(it.anomaly)} 相对基准收盘 ${it.anomaly.ref} ${moveText(it.anomaly)}（约平时波动的 ${it.anomaly.mult.toFixed(1)} 倍），请一并解释。`]
    : [];
  if (it.status === 'released') {
    return [
      `${it.ticker} 的 ${it.period}财报已于 ${when}发布。请做财报后核对：`,
      ...filed,
      `1. 读 ${fw} 的现行结论和 Assumptions。`,
      '2. 拉本期财报、管理层指引和电话会要点（注明来源）。',
      '3. 逐条对照仍为 active 的假设：确认 / 削弱 / 证伪，给出数字证据和来源。',
      '4. 指出 §3 可演算块里哪些输入需要改，估计 L/M/H 大致往哪边移、为什么。',
      '5. 给结论：框架是否需要修订、改哪几处。',
      '',
      tldr('框架要不要改、哪几条假设被削弱或证伪、L/M/H 大致往哪边移', '逐条核对和证据'),
      '只在对话里讨论，不写文件；落盘等我说「可以落盘」，再走 sediment-framework。',
    ].join('\n');
  }
  return [
    `${it.ticker} 将于 ${when}发布 ${it.period}财报。请做财报前准备：`,
    ...filed,
    `1. 读 ${fw} 的现行结论和 Assumptions。`,
    '2. 列出这次财报会检验的假设；每条写清看哪个数字、到什么程度算确认、到什么程度算削弱。',
    '3. 对照市场一致预期和框架的中性情景（M），指出分歧最大的地方。',
    '4. 留一份财报出来后回来核对的清单。',
    '',
    tldr('这次财报主要检验哪几条假设、最该盯的几个数字、和一致预期分歧最大的地方', '逐条假设和核对清单'),
    '只在对话里讨论，不写文件。',
  ].join('\n');
}

/** The anomaly page's research prompt: find the cause, then check it against the framework. */
export function anomalyPrompt(it: AnomalyItem): string {
  const volume = it.reason === 'volume' && it.volume_mult !== null
    ? `；当天成交量是平时的 ${it.volume_mult.toFixed(1)} 倍` : '';
  return [
    `${it.ticker} 在 ${hitWhen(it)} 出现异动：相对基准收盘 ${it.ref} ${moveText(it)}（约平时波动的 ${it.mult.toFixed(1)} 倍${volume}）。请做异动核查：`,
    '1. 查清原因：新闻、公告、评级调整、行业或宏观事件（注明来源和时间）。',
    `2. 读 frameworks/${it.ticker}.md 的现行结论和 Assumptions，判断这次异动是否触及某条假设：确认 / 削弱 / 证伪，或者与基本面无关（情绪、资金面）。`,
    '3. 给结论：框架是否需要修订、改哪几处，还是只需继续观察。',
    '',
    tldr('异动原因、是否触及某条假设、框架要不要改', '来源和逐条判断'),
    '只在对话里讨论，不写文件；落盘等我说「可以落盘」，再走 sediment-framework。',
  ].join('\n');
}

/** The watch page's research prompt: is what the framework waited on out yet, and what changes if it is. */
export function watchPrompt(it: WatchItem): string {
  return [
    `${it.ticker} 的观察区卡片 ${it.due_date} 到期。之前建议先不落盘，在等：`,
    ...it.waits.map((w) => `- ${w.date} ${w.what}`),
    `到时要核对：${it.recheck}`,
    '',
    '请重新核对：',
    '1. 逐条查上面等的东西出来没有（注明来源和时间）；卖方预测要看修订日期是否在本期财报之后。',
    `2. 出来了的，读 frameworks/${it.ticker}.md 的现行结论和 Assumptions，对照新数据指出要改的地方，估计 L/M/H 大致往哪边移。`,
    '3. 还有没出来的，重新估计日子，在回复末尾附新的观察区卡片（会覆盖旧卡片，仍在等的事全部列上）；都出来了就不附。',
    '',
    tldr('哪些已经出来、框架要不要改、还在等什么', '逐条核对和来源'),
    '只在对话里讨论，不写文件；落盘等我说「可以落盘」，再走 sediment-framework。',
  ].join('\n');
}

/** An unsent draft is the user's own text: keep it and put the prompt after it, once. */
export function withPrompt(draft: string, prompt: string): string {
  if (draft.includes(prompt)) return draft; // 调研 tapped again after backing out unsent
  return draft.trim() ? `${draft.trimEnd()}\n\n${prompt}` : prompt;
}
