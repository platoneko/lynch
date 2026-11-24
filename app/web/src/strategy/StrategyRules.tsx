import { renderFragment } from '../doc/buildDoc';
import rules from './rotation-rules.md?raw';

const html = renderFragment(rules, { pool: new Set(), axes: new Set() });

export function StrategyRules() {
  return <div className="strategy-rules" dangerouslySetInnerHTML={{ __html: html }} />;
}
