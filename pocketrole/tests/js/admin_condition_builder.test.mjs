import assert from 'node:assert/strict';
import test from 'node:test';

import conditionBuilder from '../../admin/assets/admin_condition_builder.js';

const {
  applyConditionBuilderState,
  conditionJsonToBuilderState
} = conditionBuilder;

test('conditionJsonToBuilderState extracts supported fields as strings', () => {
  const state = conditionJsonToBuilderState({
    place: 'classroom',
    expected_place: 'rooftop',
    zone: 'school',
    time_from: '21:00',
    time_to: '23:00',
    min_tension: 0.4,
    required_event: '文化祭',
    multiple_characters: true,
    alone: false,
    stress_threshold_min: 0.7
  });

  assert.deepEqual(state, {
    place: 'classroom',
    expected_place: 'rooftop',
    zone: 'school',
    time_from: '21:00',
    time_to: '23:00',
    min_tension: '0.4',
    required_event: '文化祭',
    multiple_characters: true,
    alone: false,
    stress_threshold_min: '0.7'
  });
});

test('applyConditionBuilderState updates known fields and preserves unknown fields', () => {
  const condition = applyConditionBuilderState(
    { weather: 'rain', place: 'old_place' },
    {
      place: 'classroom',
      expected_place: '',
      zone: 'school',
      time_from: '21:00',
      time_to: '',
      min_tension: '0.65',
      required_event: '文化祭',
      multiple_characters: true,
      alone: false,
      stress_threshold_min: '0.5'
    }
  );

  assert.deepEqual(condition, {
    weather: 'rain',
    place: 'classroom',
    zone: 'school',
    time_from: '21:00',
    min_tension: 0.65,
    required_event: '文化祭',
    multiple_characters: true,
    alone: false,
    stress_threshold_min: 0.5
  });
});

test('applyConditionBuilderState removes blank supported fields', () => {
  const condition = applyConditionBuilderState(
    {
      place: 'classroom',
      expected_place: 'rooftop',
      zone: 'school',
      time_from: '21:00',
      time_to: '23:00',
      min_tension: 0.2,
      required_event: '文化祭',
      multiple_characters: true,
      alone: false,
      stress_threshold_min: 0.5,
      custom_flag: true
    },
    {
      place: '',
      expected_place: '',
      zone: '',
      time_from: '',
      time_to: '',
      min_tension: '',
      required_event: '',
      multiple_characters: '',
      alone: '',
      stress_threshold_min: ''
    }
  );

  assert.deepEqual(condition, { custom_flag: true });
});

test('applyConditionBuilderState rejects invalid min_tension', () => {
  assert.throws(
    () => applyConditionBuilderState({}, { min_tension: 'high' }),
    /min_tension/
  );
  assert.throws(
    () => applyConditionBuilderState({}, { min_tension: '1.5' }),
    /min_tension/
  );
});

test('applyConditionBuilderState rejects invalid stress_threshold_min', () => {
  assert.throws(
    () => applyConditionBuilderState({}, { stress_threshold_min: 'low' }),
    /stress_threshold_min/
  );
  assert.throws(
    () => applyConditionBuilderState({}, { stress_threshold_min: '-0.1' }),
    /stress_threshold_min/
  );
});
