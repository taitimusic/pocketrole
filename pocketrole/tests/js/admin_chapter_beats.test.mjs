import assert from 'node:assert/strict';
import test from 'node:test';

import chapterBeats from '../../admin/assets/admin_chapter_beats.js';

const {
  createBeatDraft,
  createBeatDrafts,
  createBeatEventDraft,
  createBeatEventDrafts,
  serializeBeatEventDrafts,
  serializeBeatDrafts
} = chapterBeats;

test('createBeatDraft normalizes beat fields for form editing', () => {
  const draft = createBeatDraft(
    {
      phase: 'setup',
      description: '導入',
      goal: '集まる',
      events: [{ type: 'notice', desc: '掲示' }]
    },
    2
  );

  assert.equal(draft.phase, 'setup');
  assert.equal(draft.description, '導入');
  assert.equal(draft.goal, '集まる');
  assert.equal(draft._beatId, 'beat_2');
  assert.equal(draft._eventsText, '[\n  {\n    "type": "notice",\n    "desc": "掲示"\n  }\n]');
});

test('createBeatDrafts keeps at least one setup beat', () => {
  assert.deepEqual(createBeatDrafts([]), [
    {
      _beatId: 'beat_0',
      phase: 'setup',
      description: '',
      goal: '',
      _eventDrafts: [],
      _eventsText: '[]'
    }
  ]);
});

test('serializeBeatDrafts returns chapter payload beats', () => {
  const beats = serializeBeatDrafts([
    {
      phase: 'climax',
      description: '山場',
      goal: '対決する',
      _eventsText: '[{"type":"notice"}]'
    }
  ]);

  assert.deepEqual(beats, [
    {
      phase: 'climax',
      description: '山場',
      goal: '対決する',
      events: [{ type: 'notice' }]
    }
  ]);
});

test('serializeBeatDrafts rejects invalid events JSON', () => {
  assert.throws(
    () => serializeBeatDrafts([{ phase: 'setup', _eventsText: '{"type":"notice"}' }]),
    /events/
  );
});

test('createBeatEventDraft preserves known and unknown event fields', () => {
  const draft = createBeatEventDraft(
    {
      type: 'director_intervention',
      desc: '掲示が変わる',
      target_char_id: 'hero',
      place_id: 'classroom',
      priority: 3,
      extra_flag: true
    },
    4
  );

  assert.equal(draft._eventId, 'event_4');
  assert.equal(draft.type, 'director_intervention');
  assert.equal(draft.desc, '掲示が変わる');
  assert.equal(draft.target_char_id, 'hero');
  assert.equal(draft.place_id, 'classroom');
  assert.equal(draft.priority, '3');
  assert.equal(draft._extraJsonText, '{\n  "extra_flag": true\n}');
});

test('serializeBeatEventDrafts merges known and unknown event fields', () => {
  const events = serializeBeatEventDrafts([
    {
      type: 'notice',
      desc: '掲示',
      target_char_id: '',
      place_id: 'classroom',
      priority: '2',
      _extraJsonText: '{"cooldown_turns": 3}'
    }
  ]);

  assert.deepEqual(events, [
    {
      cooldown_turns: 3,
      type: 'notice',
      desc: '掲示',
      place_id: 'classroom',
      priority: 2
    }
  ]);
});

test('serializeBeatEventDrafts rejects non-integer priority', () => {
  assert.throws(
    () => serializeBeatEventDrafts([{ priority: '2.5', _extraJsonText: '{}' }]),
    /priority/
  );
});

test('createBeatEventDrafts keeps an empty list empty', () => {
  assert.deepEqual(createBeatEventDrafts([]), []);
});

test('serializeBeatDrafts prefers beat event drafts when present', () => {
  const beats = serializeBeatDrafts([
    {
      phase: 'setup',
      _eventsText: '[{"type":"from_json"}]',
      _eventDrafts: [
        {
          type: 'from_cards',
          desc: 'card event',
          _extraJsonText: '{}'
        }
      ]
    }
  ]);

  assert.deepEqual(beats[0].events, [{ type: 'from_cards', desc: 'card event' }]);
});
