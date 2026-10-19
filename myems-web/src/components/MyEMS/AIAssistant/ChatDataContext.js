import React, { useState } from 'react';
import { Alert, Button, Input, Label } from 'reactstrap';

const statusLabels = {
  available: 'Available',
  partial: 'Partial data',
  no_records: 'No records',
  forbidden: 'Not authorized',
  failed: 'Query failed',
  input_required: 'Select analysis scope',
  unavailable: 'Historical evidence unavailable',
  read_only: 'Read-only analysis',
  disabled: 'System analysis disabled',
  unsupported: 'Unsupported query'
};

export const scopeSelection = context => {
  const scope = (context && context.scope) || {};
  const selection = {};
  if (scope.space_id) selection.space_id = scope.space_id;
  if (scope.product_ids && scope.product_ids.length === 1) selection.product_id = scope.product_ids[0];
  if (scope.meter_key) selection.meter_key = scope.meter_key;
  if (scope.period_start && scope.period_end) {
    selection.period_start = scope.period_start;
    selection.period_end = scope.period_end;
  }
  return selection;
};

export function evidenceFacts(value, path = '') {
  if (!value || typeof value !== 'object') return [];
  const facts = [];
  if (value.source) facts.push({ label: path, value: value.source });
  if (value.coverage) {
    const coverage = value.coverage;
    facts.push({ label: 'Observed days / requested days',
      value: `${coverage.observed_days} / ${coverage.requested_days}` });
    if (coverage.first_local) facts.push({ label: 'Observed period',
      value: `${coverage.first_local} ~ ${coverage.last_local}` });
  }
  if (value.total !== undefined && value.total !== null) {
    facts.push({ label: path || 'Total', value: `${value.total} ${value.unit || ''}` });
  }
  [
    ['current_factor_carbon_kgco2e', 'Current factor carbon emissions', 'kgCO2e'],
    ['recorded_carbon_kgco2e', 'Recorded carbon emissions', 'kgCO2e'],
    ['current_factor_energy_tce', 'Standard coal equivalent', 'tce'],
    ['footprint_per_functional_unit', 'Footprint intensity', value.display_unit],
    ['recorded_activity_total', 'Recorded activity emissions', 'kgCO2e'],
    ['recorded_emissions_tco2e', 'Recorded carbon emissions', 'tCO2e'],
    ['page_balance_tco2e', 'Page balance', 'tCO2e']
  ].forEach(([key, label, unit]) => {
    if (value[key] !== undefined && value[key] !== null) {
      facts.push({ label, value: `${value[key]} ${unit || ''}` });
    }
  });
  Object.entries(value).forEach(([key, item]) => {
    if (key !== 'coverage' && item && typeof item === 'object') {
      const label = item.name || item.product_name || path;
      facts.push(...evidenceFacts(item, label));
    }
  });
  return facts;
}

export function ChatEvidence({ context, t }) {
  if (!context) return null;
  const scope = context.scope || {};
  return <details className="ai-data-evidence">
    <summary>{t('Analysis data sources')} · {t(statusLabels[context.status] || context.status)}</summary>
    {context.message && <p>{context.message}</p>}
    {scope.space_name && <div>{t('Space')}: {scope.space_name}</div>}
    {scope.products && <div>{t('Product')}: {scope.products.map(product => product.name).join(', ')}</div>}
    {scope.meter && <div>{t('Meter')}: {scope.meter.name} ({scope.meter_key})</div>}
    {scope.period_start && <div>{t('Report period')}: {scope.period_start} ~ {scope.period_end}
      {' '}(UTC{scope.timezone_offset || '+08:00'})</div>}
    {context.generated_at_utc && <div>{t('Snapshot time')}: {context.generated_at_utc} UTC</div>}
    {(context.sections || []).map((section, index) => <div className="mt-2" key={index}>
      <strong>{section.label} · {t(statusLabels[section.status] || section.status)}</strong>
      <div>{section.period_start} ~ {section.period_end} ({t(section.period)})</div>
      {section.scope_note && <div>{section.scope_note}</div>}
      {section.message && <div>{section.message}</div>}
      {section.data && <>
        <ul>{evidenceFacts(section.data).map((fact, i) =>
          <li key={i}>{t(fact.label)}: {fact.value}</li>)}</ul>
        {section.data.note && <p>{section.data.note}</p>}
        <details><summary>{t('Metric and rule details')}</summary>
          <pre>{JSON.stringify(section.data, null, 2)}</pre>
        </details>
      </>}
    </div>)}
    {context.detail_notice && <Alert color="warning">{context.detail_notice}</Alert>}
  </details>;
}

export function ChatClarification({ pending, disabled, onSubmit, t }) {
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  if (!pending) return null;
  const context = pending.context;
  return <Alert color="info" className="m-3">
    <strong>{t('Select analysis scope')}</strong>
    <p>{context.message}</p>
    {(context.options || []).map(option => <Button color="primary" outline size="sm"
      className="mr-2 mb-2" disabled={disabled} key={option.value}
      onClick={() => onSubmit({ [context.field]: option.value })}>{option.label}</Button>)}
    {context.field === 'period' && <div className="d-flex flex-wrap align-items-end">
      <Label className="mr-2">{t('Report period start')}
        <Input type="date" value={start} disabled={disabled} onChange={event => setStart(event.target.value)} />
      </Label>
      <Label className="mr-2">{t('Report period end')}
        <Input type="date" value={end} disabled={disabled} onChange={event => setEnd(event.target.value)} />
      </Label>
      <Button className="mb-2" color="primary" disabled={disabled || !start || !end || start > end}
        onClick={() => onSubmit({ period_start: start, period_end: end })}>{t('Continue analysis')}</Button>
    </div>}
  </Alert>;
}
