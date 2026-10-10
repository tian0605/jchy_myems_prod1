import React from 'react';
import { Badge, Card, CardBody, CardHeader, Table } from 'reactstrap';
import ButtonIcon from '../../common/ButtonIcon';

const parseTableRow = line => {
  const cells = [];
  let cell = '';
  let escaped = false;
  Array.from(line.trim()).forEach((character, index, characters) => {
    if (escaped) {
      cell += character;
      escaped = false;
    } else if (character === '\\' && characters[index + 1] === '|') {
      escaped = true;
    } else if (character === '|') {
      cells.push(cell.trim());
      cell = '';
    } else {
      cell += character;
    }
  });
  cells.push(cell.trim());
  if (line.trim().startsWith('|')) cells.shift();
  if (line.trim().endsWith('|') && !line.trim().endsWith('\\|')) cells.pop();
  return cells;
};

const tableAlignment = cell => {
  const value = cell.trim();
  const alignsLeft = value.startsWith(':');
  const alignsRight = value.endsWith(':');
  if (alignsLeft && alignsRight) return 'center';
  if (alignsRight) return 'right';
  return 'left';
};

const isTableSeparator = line => {
  const cells = parseTableRow(line);
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
};

export const ReportContent = ({ content }) => {
  const lines = (content || '').split('\n');
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim().includes('|') && index + 1 < lines.length && isTableSeparator(lines[index + 1])) {
      const tableLines = [];
      while (index < lines.length && lines[index].trim().includes('|')) {
        tableLines.push(lines[index]);
        index += 1;
      }
      const rows = tableLines.filter(tableLine => !isTableSeparator(tableLine)).map(parseTableRow);
      if (rows.length > 1) {
        const columnCount = Math.max(...rows.map(row => row.length));
        const normalizedRows = rows.map(row => row.concat(Array(Math.max(0, columnCount - row.length)).fill('')));
        const alignments = parseTableRow(tableLines.find(isTableSeparator)).map(tableAlignment);
        blocks.push(
          <div className="table-responsive ai-report-table-wrap mb-3" key={'table-' + index}>
            <Table bordered size="sm" className="ai-report-table mb-0">
              <thead><tr>{normalizedRows[0].map((cell, cellIndex) => <th key={cellIndex} scope="col" style={{ textAlign: alignments[cellIndex] || 'left' }}>{cell}</th>)}</tr></thead>
              <tbody>{normalizedRows.slice(1).map((row, rowIndex) => (
                <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex} style={{ textAlign: alignments[cellIndex] || 'left' }}>{cell}</td>)}</tr>
              ))}</tbody>
            </Table>
          </div>
        );
      }
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const Heading = 'h' + Math.min(heading[1].length + 2, 5);
      blocks.push(<Heading className="mt-3 mb-2" key={'heading-' + index}>{heading[2]}</Heading>);
    } else if (line.trim()) {
      blocks.push(<p className="mb-2" key={'paragraph-' + index}>{line}</p>);
    }
    index += 1;
  }
  return <>{blocks}</>;
};

export const ReportStreaming = ({ content, reportType, status, t }) => (
  <Card className="ai-report-card" style={{ maxWidth: '100%' }}>
    <CardHeader className="ai-report-card-header d-flex align-items-center">
      <span className="spinner-border spinner-border-sm mr-2" role="status" />
      {status || t('Generating report')} · {content.length} {t('Characters')}
    </CardHeader>
    <CardBody style={{ maxHeight: '54vh', overflowY: 'auto' }}>
      <Badge className="ai-report-type-badge mb-2">{reportType === 'ghg_emissions' ? t('GHG emissions report') : t('Product carbon footprint report')}</Badge>
      {content ? <ReportContent content={content} /> : <span className="text-muted">{status || t('Preparing report')}</span>}
    </CardBody>
  </Card>
);

export const ReportCard = ({ report, onPreview, onDownload, onRefine, t }) => (
  <Card className="ai-report-card" style={{ width: 'min(100%, 430px)' }}>
    <CardHeader className="ai-report-card-header d-flex align-items-center">
      <ButtonIcon icon="file-alt" color="primary" size="sm" className="mr-2" disabled />
      <div className="flex-grow-1">
        <strong>{report.type === 'ghg_emissions' ? t('GHG emissions report') : t('Product carbon footprint report')}</strong>
        <div className="small text-muted">{t('Version')} #{report.id} · {report.mode === 'real_data' ? t('Real data') : t('Estimate draft')}</div>
        {report.parent_report_version_id && <div className="small text-muted">{t('Based on version')} #{report.parent_report_version_id}</div>}
      </div>
    </CardHeader>
    <CardBody>
      <div className="small text-muted mb-3">{report.created_datetime_utc ? new Date(report.created_datetime_utc).toLocaleString() : ''}</div>
      <div className="d-flex flex-wrap" style={{ gap: '6px' }}>
        <ButtonIcon icon="eye" color="primary" size="sm" className="mr-2 flex-grow-1" onClick={() => onPreview(report)}>
          {t('Preview')}
        </ButtonIcon>
        <ButtonIcon icon="arrow-down" color="outline-secondary" size="sm" className="flex-grow-1" onClick={() => onDownload(report)}>
          {t('Download Word')}
        </ButtonIcon>
        <ButtonIcon icon="pen" color="outline-primary" size="sm" className="flex-grow-1" onClick={() => onRefine(report)}>
          {t('Continue refining')}
        </ButtonIcon>
      </div>
    </CardBody>
  </Card>
);