import { get } from '../api';
import type { StrategySnapshot } from './model';

export async function fetchStrategy(signal: AbortSignal): Promise<StrategySnapshot> {
  return (await get('/api/strategies/chinext-dividend-rotation', signal)).json();
}
