(function chapterBeatsFactory(root) {
  const EVENT_KNOWN_FIELDS = new Set([
    'type',
    'desc',
    'description',
    'target_char_id',
    'place_id',
    'priority'
  ]);

  function createBeatDraft(beat = {}, index = 0) {
    const events = Array.isArray(beat.events) ? beat.events : [];
    return {
      _beatId: beat._beatId || `beat_${index}`,
      phase: String(beat.phase || '').trim() || 'setup',
      description: String(beat.description || ''),
      goal: String(beat.goal || ''),
      _eventDrafts: createBeatEventDrafts(events),
      _eventsText: beat._eventsText !== undefined
        ? String(beat._eventsText)
        : JSON.stringify(events, null, 2)
    };
  }

  function createBeatDrafts(beats = []) {
    const source = Array.isArray(beats) && beats.length > 0
      ? beats
      : [{ phase: 'setup', description: '', goal: '', events: [] }];
    return source.map((beat, index) => createBeatDraft(beat, index));
  }

  function parseEvents(text) {
    const parsed = JSON.parse(text || '[]');
    if (!Array.isArray(parsed)) {
      throw new Error('events は JSON array で入力してください。');
    }
    return parsed;
  }

  function createBeatEventDraft(event = {}, index = 0) {
    const extra = {};
    Object.entries(event || {}).forEach(([key, value]) => {
      if (!EVENT_KNOWN_FIELDS.has(key)) {
        extra[key] = value;
      }
    });
    return {
      _eventId: event._eventId || `event_${index}`,
      type: String(event.type || ''),
      desc: String(event.desc || event.description || ''),
      target_char_id: String(event.target_char_id || ''),
      place_id: String(event.place_id || ''),
      priority: event.priority === undefined || event.priority === null ? '' : String(event.priority),
      _extraJsonText: event._extraJsonText !== undefined
        ? String(event._extraJsonText)
        : JSON.stringify(extra, null, 2)
    };
  }

  function createBeatEventDrafts(events = []) {
    if (!Array.isArray(events)) return [];
    return events.map((event, index) => createBeatEventDraft(event, index));
  }

  function parseExtraEventJson(text) {
    const parsed = JSON.parse(text || '{}');
    if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new Error('event extra は JSON object で入力してください。');
    }
    return parsed;
  }

  function serializeBeatEventDrafts(drafts = []) {
    if (!Array.isArray(drafts)) {
      throw new Error('events は JSON array で入力してください。');
    }
    return drafts.map((draft) => {
      const event = parseExtraEventJson(draft._extraJsonText || '{}');
      const type = String(draft.type || '').trim();
      const desc = String(draft.desc || '').trim();
      const targetCharId = String(draft.target_char_id || '').trim();
      const placeId = String(draft.place_id || '').trim();
      const priorityText = String(draft.priority || '').trim();
      if (type) event.type = type;
      if (desc) event.desc = desc;
      if (targetCharId) event.target_char_id = targetCharId;
      if (placeId) event.place_id = placeId;
      if (priorityText) {
        const priority = Number(priorityText);
        if (!Number.isInteger(priority)) {
          throw new Error('event priority は整数で入力してください。');
        }
        event.priority = priority;
      } else {
        delete event.priority;
      }
      return event;
    });
  }

  function serializeBeatDrafts(drafts = []) {
    if (!Array.isArray(drafts) || drafts.length === 0) {
      throw new Error('beats は少なくとも1件必要です。');
    }
    return drafts.map((draft) => ({
      phase: String(draft.phase || '').trim(),
      description: String(draft.description || '').trim(),
      goal: String(draft.goal || '').trim(),
      events: Array.isArray(draft._eventDrafts)
        ? serializeBeatEventDrafts(draft._eventDrafts)
        : parseEvents(draft._eventsText || '[]')
    }));
  }

  const api = {
    createBeatDraft,
    createBeatDrafts,
    createBeatEventDraft,
    createBeatEventDrafts,
    serializeBeatEventDrafts,
    serializeBeatDrafts
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.POCKETROLE_CHAPTER_BEATS = api;
  }
}(typeof window !== 'undefined' ? window : globalThis));
