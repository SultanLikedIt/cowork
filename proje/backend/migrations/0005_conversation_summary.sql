-- Running summary of a conversation's older turns, built incrementally as
-- context assembly would otherwise have to drop them (see
-- main._build_conversation_context). summarized_through_created_at marks the
-- newest message already folded into the summary, so the next summarization
-- pass only has to cover messages after that point instead of redoing the
-- whole history.
ALTER TABLE conversations ADD COLUMN summary TEXT;
ALTER TABLE conversations ADD COLUMN summarized_through_created_at REAL;
