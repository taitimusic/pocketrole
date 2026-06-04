(function conditionBuilderFactory(root) {
  const STRING_FIELDS = [
    'place',
    'expected_place',
    'zone',
    'time_from',
    'time_to',
    'required_event'
  ];
  const BOOLEAN_FIELDS = ['multiple_characters', 'alone'];
  const NUMBER_FIELDS = ['min_tension', 'stress_threshold_min'];

  function normalizeText(value) {
    if (value === null || value === undefined) return '';
    return String(value);
  }

  function conditionJsonToBuilderState(condition) {
    const source = condition && typeof condition === 'object' && !Array.isArray(condition)
      ? condition
      : {};
    return {
      place: normalizeText(source.place),
      expected_place: normalizeText(source.expected_place),
      zone: normalizeText(source.zone),
      time_from: normalizeText(source.time_from),
      time_to: normalizeText(source.time_to),
      min_tension: normalizeText(source.min_tension),
      required_event: normalizeText(source.required_event),
      multiple_characters: booleanStateValue(source.multiple_characters),
      alone: booleanStateValue(source.alone),
      stress_threshold_min: normalizeText(source.stress_threshold_min)
    };
  }

  function booleanStateValue(value) {
    if (value === true || value === false) return value;
    return '';
  }

  function applyConditionBuilderState(condition, state) {
    const result = condition && typeof condition === 'object' && !Array.isArray(condition)
      ? { ...condition }
      : {};
    const nextState = state && typeof state === 'object' && !Array.isArray(state) ? state : {};

    STRING_FIELDS.forEach((field) => {
      const value = normalizeText(nextState[field]).trim();
      if (value) {
        result[field] = value;
      } else {
        delete result[field];
      }
    });

    BOOLEAN_FIELDS.forEach((field) => {
      const value = nextState[field];
      if (value === true || value === false) {
        result[field] = value;
      } else {
        delete result[field];
      }
    });

    NUMBER_FIELDS.forEach((field) => {
      const numberText = normalizeText(nextState[field]).trim();
      if (numberText) {
        const numberValue = Number.parseFloat(numberText);
        if (!Number.isFinite(numberValue) || numberValue < 0 || numberValue > 1) {
          throw new Error(`${field} は 0.0 から 1.0 の数値で入力してください。`);
        }
        result[field] = numberValue;
      } else {
        delete result[field];
      }
    });

    return result;
  }

  const api = {
    applyConditionBuilderState,
    conditionJsonToBuilderState
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.POCKETROLE_CONDITION_BUILDER = api;
  }
}(typeof window !== 'undefined' ? window : globalThis));
