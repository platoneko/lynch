/** One thing a ticker's framework update waits on, and the day it is expected (YYYY-MM-DD, Beijing). */
export interface WatchWait {
  what: string;
  date: string;
}

/** The card a 讨论 reply ends with (server chat/watch.py), waits soonest first. */
export interface WatchCard {
  ticker: string;
  waits: WatchWait[];
  recheck: string; // the numbers or assumptions to check again once it is out
}

/** A row of the watch list: the ticker's saved card. */
export interface WatchItem extends WatchCard {
  saved_at: number;
  conv_id: string;
  due_date: string; // the earliest wait's day
  due: boolean;
  researched: boolean; // due, and a 讨论 chat on the ticker started on or after the due day
}

export interface WatchList {
  items: WatchItem[]; // soonest first
}
