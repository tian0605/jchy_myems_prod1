import React from 'react';
import ReactDOM from 'react-dom';
import { act, Simulate } from 'react-dom/test-utils';
import { ChatClarification, ChatEvidence, evidenceFacts, scopeSelection } from './ChatDataContext';

const t = key => key;
let container;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => { ReactDOM.unmountComponentAtNode(container); });
  container.remove();
});

test('scope selection retains namespaced meter and confirmed dates', () => {
  expect(scopeSelection({ scope: { space_id: 3, product_ids: [3], meter_key: 'offline:12',
    period_start: '2026-06-01', period_end: '2026-06-30' } })).toEqual({
    space_id: 3, product_id: 3, meter_key: 'offline:12',
    period_start: '2026-06-01', period_end: '2026-06-30'
  });
  expect(scopeSelection({ scope: { product_ids: [3, 4] } })).toEqual({});
});

test('zero totals and partial coverage remain visible', () => {
  const facts = evidenceFacts({ source: 'test source', total: 0, unit: 'kWh',
    coverage: { observed_days: 14, requested_days: 30 } });
  expect(facts).toContainEqual({ label: 'Total', value: '0 kWh' });
  expect(facts).toContainEqual({ label: 'Observed days / requested days', value: '14 / 30' });
});

test('sources render as text, not executable markup', () => {
  act(() => { ReactDOM.render(<ChatEvidence t={t} context={{
    status: 'partial', scope: { space_name: '<img src=x onerror=alert(1)>' },
    sections: [{ type: 'energy', label: 'Energy', status: 'partial',
      data: { source: 'test source', total: '993200', unit: 'kWh',
        coverage: { observed_days: 14, requested_days: 30 } } }]
  }} />, container); });
  expect(container.textContent).toContain('993200 kWh');
  expect(container.textContent).toContain('14 / 30');
  expect(container.textContent).toContain('<img');
  expect(container.querySelector('img')).toBeNull();
});

test('product clarification preserves numeric option IDs', () => {
  const submit = jest.fn();
  act(() => { ReactDOM.render(<ChatClarification t={t} onSubmit={submit} pending={{
    context: { field: 'product_id', message: 'Choose product', options: [{ value: 3, label: 'Product' }] }
  }} />, container); });
  act(() => Simulate.click(container.querySelector('button')));
  expect(submit).toHaveBeenCalledWith({ product_id: 3 });
});

test('period clarification prevents invalid ranges', () => {
  const submit = jest.fn();
  act(() => { ReactDOM.render(<ChatClarification t={t} onSubmit={submit} pending={{
    context: { field: 'period', message: 'Choose dates' }
  }} />, container); });
  const inputs = container.querySelectorAll('input');
  expect(container.querySelector('button').disabled).toBe(true);
  act(() => {
    Simulate.change(inputs[0], { target: { value: '2026-06-30' } });
    Simulate.change(inputs[1], { target: { value: '2026-06-01' } });
  });
  expect(container.querySelector('button').disabled).toBe(true);
  act(() => Simulate.change(inputs[0], { target: { value: '2026-06-01' } }));
  act(() => Simulate.click(container.querySelector('button')));
  expect(submit).toHaveBeenCalledWith({ period_start: '2026-06-01', period_end: '2026-06-01' });
});

test('historical redaction and pending controls honor state', () => {
  act(() => { ReactDOM.render(<ChatEvidence t={t} context={{ status: 'unavailable' }} />, container); });
  expect(container.textContent).toContain('Historical evidence unavailable');
  act(() => { ReactDOM.render(<ChatClarification t={t} disabled onSubmit={jest.fn()} pending={{
    context: { field: 'space_id', options: [{ value: 3, label: 'Space' }] }
  }} />, container); });
  expect(container.querySelector('button').disabled).toBe(true);
});

test('annual source scope is distinct from the requested month', () => {
  act(() => {
    ReactDOM.render(<ChatEvidence t={t} context={{
      status: 'partial',
      scope: { period_start: '2026-06-01', period_end: '2026-06-30' },
      sections: [{
        type: 'assets', label: 'Assets', status: 'partial', period: 'reporting',
        period_start: '2026-01-01', period_end: '2026-12-31',
        scope_note: 'Annual accounting query, not only the requested month.'
      }]
    }} />, container);
  });
  expect(container.textContent).toContain('2026-06-01 ~ 2026-06-30');
  expect(container.textContent).toContain('2026-01-01 ~ 2026-12-31');
  expect(container.textContent).toContain('Annual accounting query');
});
