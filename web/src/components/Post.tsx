import { useEffect, useState } from "react";
import { api, type PostT } from "../api";
import { Avatar, FORMAT_LABEL, PostBody, StanceChip, useWorld } from "../ctx";

export function VoteBox({ post }: { post: PostT }) {
  const { world, toast, state } = useWorld();
  const [score, setScore] = useState<number | null>(null);
  const mine = post.author === state.human_id;
  useEffect(() => setScore(null), [post.score]);
  const vote = async (v: 1 | -1) => {
    try {
      const r = await api.vote(world, post.id, v);
      setScore(r.score);
    } catch (e) { toast((e as Error).message, true); }
  };
  return (
    <div className="vote" title={mine ? "You can't vote on your own post" : "Vote as yourself"}>
      <button className="up" disabled={mine} onClick={() => vote(1)} aria-label="upvote">▲</button>
      <span className="score">{score ?? post.score}</span>
      <button className="down" disabled={mine} onClick={() => vote(-1)} aria-label="downvote">▼</button>
    </div>
  );
}

function ReplyBox({ post, onDone }: { post: PostT; onDone: () => void }) {
  const { world, toast, refresh } = useWorld();
  const [text, setText] = useState("");
  const [agree, setAgree] = useState(0);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      await api.reply(world, post.id, text.trim(), agree);
      setText(""); onDone(); refresh();
    } catch (e) { toast((e as Error).message, true); }
    setBusy(false);
  };
  return (
    <div className="stack" style={{ marginTop: 8 }}>
      <textarea className="textarea" autoFocus maxLength={400} placeholder={`Reply to ${post.handle}…`}
        value={text} onChange={(e) => setText(e.target.value)} />
      <div className="row spread wrap">
        <div className="segmented" style={{ width: 240 }}>
          {[[-1, "Disagree"], [0, "Neutral"], [1, "Agree"]].map(([v, l]) => (
            <button key={v} className={agree === v ? "on" : ""} onClick={() => setAgree(v as number)}>{l}</button>
          ))}
        </div>
        <div className="row">
          <button className="btn ghost sm" onClick={onDone}>Cancel</button>
          <button className="btn primary sm" disabled={busy || text.trim().length < 2} onClick={send}>Reply</button>
        </div>
      </div>
    </div>
  );
}

function Reply({ post, depth }: { post: PostT; depth: number }) {
  const { openProfile, freshIds } = useWorld();
  const [replying, setReplying] = useState(false);
  return (
    <div className={`reply ${freshIds.has(post.id) ? "post fresh" : ""}`} style={freshIds.has(post.id) ? { padding: 6, borderRadius: 10, border: "1px solid transparent" } : undefined}>
      <Avatar id={post.author} handle={post.handle} emoji={post.emotion} size="sm" human={post.human} />
      <div className="grow">
        <div className="post-head small">
          <span className="handle" onClick={() => openProfile(post.author)}>{post.human ? "You" : post.handle}</span>
          <StanceChip value={post.agreement} reply />
          <span className="muted tiny">P{post.period}</span>
        </div>
        <PostBody text={post.text} format={post.format} small />
        <div className="post-actions small">
          <VoteBox post={post} />
          <button className="btn ghost sm" onClick={() => setReplying((r) => !r)}>Reply</button>
        </div>
        {replying && <ReplyBox post={post} onDone={() => setReplying(false)} />}
        {post.children && post.children.length > 0 && depth < 5 && (
          <div className="replies">{post.children.map((c) => <Reply key={c.id} post={c} depth={depth + 1} />)}</div>
        )}
      </div>
    </div>
  );
}

export function PostCard({ post, full }: { post: PostT; full?: boolean }) {
  const { openProfile, openThread, freshIds, state } = useWorld();
  const [replying, setReplying] = useState(false);
  const kids = post.children ?? [];
  const shown = full ? kids : kids.slice(-3);
  const author = state.agents[post.author];
  return (
    <article className={`card post ${freshIds.has(post.id) ? "fresh" : ""}`}>
      <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
        <Avatar id={post.author} handle={post.handle} emoji={post.emotion} human={post.human} />
        <div className="grow">
          <div className="post-head">
            <span className="handle" onClick={() => openProfile(post.author)}>{post.human ? "You" : post.handle}</span>
            {author && !post.human && <span className="muted small">{author.job}</span>}
            <span className="muted small">· period {post.period}</span>
          </div>
          <div className="row wrap" style={{ gap: 6, marginTop: 4 }}>
            {post.community_name && <span className="chip accent">{post.community_name}</span>}
            <span className="chip">#{post.topic}</span>
            {FORMAT_LABEL[post.format] && <span className="chip">{FORMAT_LABEL[post.format]}</span>}
            <StanceChip value={post.stance} />
          </div>
          {post.happening && (
            <div className="story-ref" title="This post reacts to a story from the platform's newsroom">
              <span>{post.happening.icon}</span><span className="grow">{post.happening.headline}</span>
            </div>
          )}
          <PostBody text={post.text} format={post.format} />
          <div className="post-actions">
            <VoteBox post={post} />
            <button className="btn ghost sm" onClick={() => setReplying((r) => !r)}>💬 Reply</button>
            {!full && <button className="btn ghost sm" onClick={() => openThread(post.id)}>{post.replies} {post.replies === 1 ? "reply" : "replies"}</button>}
          </div>
          {replying && <ReplyBox post={post} onDone={() => setReplying(false)} />}
          {shown.length > 0 && (
            <div className="replies">
              {!full && kids.length > shown.length && (
                <button className="btn ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => openThread(post.id)}>
                  View {kids.length - shown.length} earlier replies
                </button>
              )}
              {shown.map((c) => <Reply key={c.id} post={c} depth={1} />)}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
