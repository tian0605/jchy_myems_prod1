import React, { useEffect, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Col,
  FormGroup,
  Input,
  Label,
  Modal,
  ModalBody,
  ModalHeader,
  Nav,
  NavItem,
  NavLink,
  Row,
  Spinner
} from 'reactstrap';
import { withTranslation } from 'react-i18next';
import withRedirect from '../../../hoc/withRedirect';
import { APIBaseURL, settings } from '../../../config';
import { createCookie, getCookieValue } from '../../../helpers/utils';
import ButtonIcon from '../../common/ButtonIcon';
import { ReportCard, ReportContent, ReportStreaming } from './AIAssistantReport';
import { ChatEvidence, ChatClarification, scopeSelection } from './ChatDataContext';
import { requestAIStream } from './AIAssistantStream';
import './AIAssistant.css';

const AIAssistant = ({ setRedirect, setRedirectUrl, t }) => {
  const [conversations, setConversations] = useState([]);
  const [activeConversation, setActiveConversation] = useState(null);
  const [activeTab, setActiveTab] = useState('chat');
  const [models, setModels] = useState([]);
  const [content, setContent] = useState('');
  const [useSystemData, setUseSystemData] = useState(true);
  const [reportType, setReportType] = useState('');
  const [reportMode, setReportMode] = useState('real_data');
  const [includeCostData, setIncludeCostData] = useState(false);
  const [attachments, setAttachments] = useState([]);
  const [selectedAttachmentIds, setSelectedAttachmentIds] = useState([]);
  const [reportInputOptions, setReportInputOptions] = useState(null);
  const [reportDrafts, setReportDrafts] = useState({});
  const [reportVersions, setReportVersions] = useState([]);
  const [streamingReport, setStreamingReport] = useState(null);
  const [lastReportRequest, setLastReportRequest] = useState(null);
  const [revisionTarget, setRevisionTarget] = useState(null);
  const [revisionContent, setRevisionContent] = useState('');
  const [previewReport, setPreviewReport] = useState(null);
  const [conversationSearch, setConversationSearch] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState('');
  const [chatPending, setChatPending] = useState(null);
  const pendingRequest = useRef(null);
  useEffect(() => () => {
    if (pendingRequest.current) pendingRequest.current.abort();
  }, []);

  const reportDraftKey = activeConversation && reportType
    ? [activeConversation.id, reportType, reportMode].join(':') : '';
  const reportContext = reportDraftKey ? (reportDrafts[reportDraftKey] || {}) : {};

  const requestOptions = method => ({
    method,
    headers: {
      'Content-type': 'application/json',
      'User-UUID': getCookieValue('user_uuid'),
      Token: getCookieValue('token')
    }
  });

  const refreshLogin = () => {
    const isLoggedIn = getCookieValue('is_logged_in');
    const userName = getCookieValue('user_name');
    const displayName = getCookieValue('user_display_name');
    const userUuid = getCookieValue('user_uuid');
    const token = getCookieValue('token');
    if (!isLoggedIn) {
      setRedirectUrl('/authentication/basic/login');
      setRedirect(true);
      return false;
    }
    createCookie('is_logged_in', true, settings.cookieExpireTime);
    createCookie('user_name', userName, settings.cookieExpireTime);
    createCookie('user_display_name', displayName, settings.cookieExpireTime);
    createCookie('user_uuid', userUuid, settings.cookieExpireTime);
    createCookie('token', token, settings.cookieExpireTime);
    return true;
  };

  const loadConversation = conversationId => {
    setChatPending(null);
    setError('');
    return fetch(APIBaseURL + '/ai-assistant/conversations/' + conversationId, requestOptions('GET'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to load conversation'));
        }
        return response.json();
      })
      .then(json => {
        setActiveConversation(json);
        const last = json.messages[json.messages.length - 1];
        const question = json.messages.slice().reverse().find(message => message.role === 'user');
        if (last && question && last.data_context && last.data_context.status === 'input_required') {
          setChatPending({ question: question.content, context: last.data_context,
            selection: scopeSelection(last.data_context) });
        }
        setSelectedAttachmentIds([]);
        return Promise.all([
          fetch(APIBaseURL + '/ai-assistant/conversations/' + conversationId + '/attachments', requestOptions('GET')),
          fetch(APIBaseURL + '/ai-assistant/conversations/' + conversationId + '/report-versions', requestOptions('GET'))
        ]);
      })
      .then(responses => Promise.all(responses.map(response => (response.ok ? response.json() : []))))
      .then(results => {
        setAttachments(results[0]);
        setReportVersions(results[1]);
        setRevisionTarget(null);
        setRevisionContent('');
      })
      .catch(loadError => setError(loadError.message));
  };

  const loadConversations = () =>
    fetch(APIBaseURL + '/ai-assistant/conversations', requestOptions('GET'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to load conversations'));
        }
        return response.json();
      })
      .then(json => {
        setConversations(json);
        return json;
      });

  useEffect(() => {
    if (!refreshLogin()) {
      return;
    }
    Promise.all([
      loadConversations(),
      fetch(APIBaseURL + '/ai-assistant/models', requestOptions('GET')).then(response =>
        response.ok ? response.json() : []
      )
    ])
      .then(results => {
        setModels(results[1]);
        if (results[0].length > 0) {
          return loadConversation(results[0][0].id);
        }
        return null;
      })
      .catch(loadError => setError(loadError.message))
      .finally(() => setIsLoading(false));
    // Initial data must only be loaded once after the login state is refreshed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!reportType || !activeConversation) {
      setReportInputOptions(null);
      return;
    }
    let isCurrentRequest = true;
    const draftKey = [activeConversation.id, reportType, reportMode].join(':');
    setError('');
    fetch(APIBaseURL + '/ai-assistant/report-inputs?report_type=' + reportType + '&report_mode=' + reportMode,
      requestOptions('GET'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to load report options'));
        }
        return response.json();
      })
      .then(options => {
        if (!isCurrentRequest) {
          return;
        }
        setReportInputOptions(options);
        setReportDrafts(previous => previous[draftKey]
          ? previous
          : { ...previous, [draftKey]: options.defaults || {} });
      })
      .catch(loadError => {
        if (isCurrentRequest) {
          setError(loadError.message);
        }
      });
    return () => {
      isCurrentRequest = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportType, reportMode, activeConversation && activeConversation.id]);

  const createConversation = () => {
    if (isSending) return;
    setError('');
    fetch(APIBaseURL + '/ai-assistant/conversations', {
      ...requestOptions('POST'),
      body: JSON.stringify({ title: t('New conversation') })
    })
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to create conversation'));
        }
        return response.json();
      })
      .then(json => {
        setConversations([json].concat(conversations));
        return loadConversation(json.id);
      })
      .catch(createError => setError(createError.message));
  };

  const renameConversation = conversation => {
    if (isSending) return;
    const title = window.prompt(t('Rename conversation'), conversation.title);
    if (title === null || !title.trim()) {
      return;
    }
    setError('');
    fetch(APIBaseURL + '/ai-assistant/conversations/' + conversation.id, {
      ...requestOptions('PATCH'),
      body: JSON.stringify({ title })
    })
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to rename conversation'));
        }
        return response.json();
      })
      .then(updatedConversation => {
        setConversations(previous => previous.map(item =>
          item.id === updatedConversation.id ? { ...item, title: updatedConversation.title } : item
        ));
        setActiveConversation(previous => previous && previous.id === updatedConversation.id
          ? { ...previous, title: updatedConversation.title }
          : previous);
      })
      .catch(renameError => setError(renameError.message));
  };

  const deleteConversation = conversation => {
    if (isSending) return;
    if (!window.confirm(t('Delete this conversation?'))) {
      return;
    }
    setError('');
    fetch(APIBaseURL + '/ai-assistant/conversations/' + conversation.id, requestOptions('DELETE'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to delete conversation'));
        }
        const remainingConversations = conversations.filter(item => item.id !== conversation.id);
        setConversations(remainingConversations);
        if (activeConversation && activeConversation.id === conversation.id) {
          setAttachments([]);
          setSelectedAttachmentIds([]);
          if (remainingConversations.length > 0) {
            return loadConversation(remainingConversations[0].id);
          }
          setActiveConversation(null);
        }
        return null;
      })
      .catch(deleteError => setError(deleteError.message));
  };

  const appendAssistantDelta = delta => {
    setActiveConversation(previous => {
      if (!previous) {
        return previous;
      }
      const messages = previous.messages.slice();
      const lastMessage = messages[messages.length - 1];
      if (lastMessage && lastMessage.role === 'assistant' && lastMessage.isStreaming) {
        messages[messages.length - 1] = { ...lastMessage, content: lastMessage.content + delta };
      }
      return { ...previous, messages };
    });
  };

  const updateAssistantStatus = status => {
    setActiveConversation(previous => {
      if (!previous) {
        return previous;
      }
      const messages = previous.messages.slice();
      const lastMessage = messages[messages.length - 1];
      if (lastMessage && lastMessage.role === 'assistant' && lastMessage.isStreaming) {
        messages[messages.length - 1] = { ...lastMessage, status };
      }
      return { ...previous, messages };
    });
  };

  const toggleTab = tab => {
    if (activeTab !== tab) {
      setActiveTab(tab);
    }
  };

  const uploadAttachment = event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file || !activeConversation) {
      return;
    }
    setError('');
    const formData = new FormData();
    formData.append('file', file);
    fetch(APIBaseURL + '/ai-assistant/conversations/' + activeConversation.id + '/attachments', {
      method: 'POST',
      headers: {
        'User-UUID': getCookieValue('user_uuid'),
        Token: getCookieValue('token')
      },
      body: formData
    })
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to upload attachment'));
        }
        return response.json();
      })
      .then(attachment => {
        setAttachments(previous => previous.concat([attachment]));
        setSelectedAttachmentIds(previous => previous.concat([attachment.id]));
      })
      .catch(uploadError => setError(uploadError.message));
  };

  const toggleAttachment = attachmentId => {
    setSelectedAttachmentIds(previous =>
      previous.includes(attachmentId)
        ? previous.filter(id => id !== attachmentId)
        : previous.concat([attachmentId])
    );
  };

  const downloadReport = report => {
    if (!activeConversation) {
      return;
    }
    fetch(APIBaseURL + '/ai-assistant/conversations/' + activeConversation.id + '/report-versions/' + report.id + '/download',
      requestOptions('GET'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to download report'));
        }
        return response.blob();
      })
      .then(blob => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'myems-ai-report-' + report.id + '.docx';
        link.click();
        URL.revokeObjectURL(url);
      })
      .catch(downloadError => setError(downloadError.message));
  };

  const sendMessage = (messageOverride, selectedReport, reportContextOverride, parentReportVersionId,
    includeCostDataOverride = false, chatSelection) => {
    const message = (messageOverride || content).trim();
    const selectedReportContext = reportContextOverride || (selectedReport ? reportContext : undefined);
    if (!message || !activeConversation || isSending) {
      return;
    }
    if (!selectedReport && /(生成|创建|编制|generate|create|prepare).*(报告|report)|(报告|report).*(生成|创建|编制|generate|create|prepare)/i.test(message)) {
      setActiveTab('report');
      setError(t('Use the Generate report tab to create a report.'));
      return;
    }
    if (selectedReport && !parentReportVersionId) {
      const missingReportInput = !selectedReportContext.period_start || !selectedReportContext.period_end ||
        !selectedReportContext.space_id ||
        (selectedReport.type === 'product_carbon_footprint' && !selectedReportContext.product_id);
      if (missingReportInput) {
        setError(t('Complete the following information to continue your report request.'));
        return;
      }
    }
    if (selectedReport) {
      setStreamingReport({ content: '', type: selectedReport.type, status: t('Preparing report'), context: selectedReportContext });
      setLastReportRequest({ message, report: selectedReport, context: selectedReportContext, parentReportVersionId,
        includeCostData: includeCostDataOverride });
    } else {
      setContent('');
    }
    setError('');
    setChatPending(null);
    setIsSending(true);
    if (!selectedReport) {
      const optimisticUserMessage = { id: 'local-user', role: 'user', content: message };
      const optimisticAssistantMessage = { id: 'local-assistant', role: 'assistant', content: '', isStreaming: true };
      setActiveConversation(previous => ({
        ...previous,
        messages: previous.messages.concat([optimisticUserMessage, optimisticAssistantMessage])
      }));
    }

    const controller = new AbortController();
    pendingRequest.current = controller;
    requestAIStream(APIBaseURL + '/ai-assistant/conversations/' + activeConversation.id + '/messages', {
      ...requestOptions('POST'),
      signal: controller.signal,
      body: JSON.stringify({
        content: message,
        use_system_data: useSystemData,
        attachment_ids: selectedAttachmentIds,
        report: selectedReport,
        report_context: selectedReport ? selectedReportContext : undefined,
        parent_report_version_id: parentReportVersionId,
        include_cost_data: includeCostDataOverride,
        chat_selection: selectedReport ? undefined : chatSelection
      })
    }, (eventName, data) => {
      if (eventName === 'delta') {
        if (selectedReport) {
          setStreamingReport(previous => previous && {
            ...previous, content: previous.content + data.content, status: t('Generating report')
          });
        } else {
          appendAssistantDelta(data.content);
          updateAssistantStatus(t('Generating analysis'));
        }
      } else if (eventName === 'status') {
        const status = data.stage === 'collecting_report_data'
          ? t('Collecting report data')
          : data.stage === 'model_reasoning' ? t('Model is reasoning')
          : data.stage === 'waiting_for_model' ? t('Waiting for model response')
          : t(selectedReport ? 'Generating report' : 'Generating analysis');
        if (selectedReport) {
          setStreamingReport(previous => previous && { ...previous, status });
        } else {
          updateAssistantStatus(status);
        }
      } else if (eventName === 'report_done') {
        setReportVersions(previous => previous.concat([{
          id: data.id, type: data.type, mode: data.mode, content: '',
          parent_report_version_id: data.parent_report_version_id,
          report_context: data.report_context, created_datetime_utc: new Date().toISOString()
        }]));
      } else if (eventName === 'input_required') {
        setError(t('The available report inputs changed. Review the report information and send again.'));
      } else if (eventName === 'data_context') {
        setActiveConversation(previous => ({
          ...previous, messages: previous.messages.map(item =>
            item.id === 'local-assistant' ? { ...item, data_context: data } : item)
        }));
      }
    })
      .then(() => loadConversation(activeConversation.id))
      .then(loadConversations)
      .then(() => setSelectedAttachmentIds([]))
      .catch(sendError => setError(t(sendError.name === 'AbortError' ? 'Stopped waiting for response' : sendError.message)))
      .finally(() => {
        pendingRequest.current = null;
        setStreamingReport(null);
        setActiveConversation(previous => previous && ({
          ...previous, messages: previous.messages.map(item =>
            item.id === 'local-assistant' ? { ...item, isStreaming: false } : item)
        }));
        setIsSending(false);
      });
  };

  const generateReport = () => {
    if (!reportType) {
      setError(t('Select a report type'));
      return;
    }
    if (reportContext.period_start && reportContext.period_end &&
        reportContext.period_start > reportContext.period_end) {
      setError(t('Report period start must not be after report period end.'));
      return;
    }
    sendMessage(t('Generate report'), { type: reportType, mode: reportMode }, { ...reportContext }, undefined,
      includeCostData);
  };

  const retryReport = () => {
    if (lastReportRequest) {
      sendMessage(lastReportRequest.message, lastReportRequest.report, lastReportRequest.context,
        lastReportRequest.parentReportVersionId, lastReportRequest.includeCostData);
    }
  };

  const updateReportContext = (name, value) => {
    if (!reportDraftKey) {
      return;
    }
    setReportDrafts(previousDrafts => {
      const previous = previousDrafts[reportDraftKey] || {};
      if (name !== 'space_id') {
        return { ...previousDrafts, [reportDraftKey]: { ...previous, [name]: value } };
      }
      const nextProduct = reportInputOptions && reportInputOptions.products.find(product =>
        product.space_id === Number(value));
      const currentProductIsAvailable = reportInputOptions && reportInputOptions.products.some(product =>
        product.space_id === Number(value) && product.id === Number(previous.product_id));
      return {
        ...previousDrafts,
        [reportDraftKey]: {
          ...previous,
          space_id: value,
          product_id: currentProductIsAvailable ? previous.product_id : (nextProduct ? nextProduct.id : '')
        }
      };
    });
  };

  const beginRevision = report => {
    setRevisionTarget(report);
    setRevisionContent('');
    setError('');
  };

  const submitRevision = () => {
    if (!revisionTarget || !revisionContent.trim()) {
      return;
    }
    sendMessage(revisionContent, { type: revisionTarget.type, mode: revisionTarget.mode }, undefined, revisionTarget.id);
  };

  const visibleConversations = conversations.filter(conversation =>
    conversation.title.toLowerCase().includes(conversationSearch.trim().toLowerCase())
  );

  const conversationTime = conversation => {
    if (!conversation.updated_datetime_utc) {
      return '';
    }
    return new Date(conversation.updated_datetime_utc).toLocaleDateString();
  };

  const sendOnEnter = event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  };

  if (isLoading) {
    return <Spinner color="primary" />;
  }

  return (
    <div className="ai-assistant-shell">
      <aside className="ai-conversation-sidebar" aria-label={t('AI Assistant')}>
        <div className="ai-sidebar-header">
          <strong>{t('AI Assistant')}</strong>
          <ButtonIcon icon="plus" color="primary" size="sm" title={t('New conversation')} aria-label={t('New conversation')} onClick={createConversation} />
        </div>
        <div className="ai-sidebar-search">
          <Input value={conversationSearch} onChange={event => setConversationSearch(event.target.value)} placeholder={t('Search conversations')} aria-label={t('Search conversations')} />
        </div>
        <div className="ai-conversation-list">
          {visibleConversations.map(conversation => (
            <div
              className={'ai-conversation-item ' + (activeConversation && activeConversation.id === conversation.id ? 'active' : '')}
              key={conversation.id}
              role="button"
              tabIndex="0"
              onClick={() => !isSending && loadConversation(conversation.id)}
              onKeyDown={event => {
                if (!isSending && (event.key === 'Enter' || event.key === ' ')) {
                  event.preventDefault();
                  loadConversation(conversation.id);
                }
              }}
            >
              <span className="ai-conversation-copy">
                <span className="ai-conversation-title" title={conversation.title}>{conversation.title}</span>
                <span className="ai-conversation-time">{conversationTime(conversation)}</span>
              </span>
              <span className="ai-conversation-actions">
                <ButtonIcon icon="pencil-alt" color="link" size="sm" className="p-0" title={t('Rename conversation')} aria-label={t('Rename conversation')} onClick={event => { event.stopPropagation(); renameConversation(conversation); }} />
                <ButtonIcon icon="trash-alt" color="link" size="sm" className="p-0 text-danger" title={t('Delete conversation')} aria-label={t('Delete conversation')} onClick={event => { event.stopPropagation(); deleteConversation(conversation); }} />
              </span>
            </div>
          ))}
        </div>
      </aside>
      <div className="ai-chat-workspace">
        <header className="ai-chat-header">
          <div className="ai-chat-title">{activeConversation ? activeConversation.title : t('New conversation')}</div>
          {models.length > 0 && <Badge color="primary">{models[0].name}</Badge>}
        </header>
        {error && <Alert color="danger" className="m-3 mb-0">{error}</Alert>}
        {isSending && <div className="mx-3 mt-2">
          <Button color="outline-secondary" size="sm" onClick={() => pendingRequest.current && pendingRequest.current.abort()}>
            {t('Stop waiting')}
          </Button>
        </div>}
        {!activeConversation && <Alert color="info" className="m-3">{t('Create a conversation to start')}</Alert>}
        {activeConversation && <Nav tabs className="ai-workflow-tabs">
          <NavItem><NavLink className={activeTab === 'chat' ? 'active' : ''} onClick={() => toggleTab('chat')}>{t('AI conversation')}</NavLink></NavItem>
          <NavItem><NavLink className={activeTab === 'report' ? 'active' : ''} onClick={() => toggleTab('report')}>{t('Generate report')}</NavLink></NavItem>
        </Nav>}
        {activeConversation && activeTab === 'chat' && (
          <div className="ai-chat-messages">
            {activeConversation.messages.filter(message => !message.report_version_id).map((message, index) => (
              <div className={'ai-message-row ' + (message.role === 'user' ? 'user' : 'assistant')} key={message.id || index}>
                <div className="ai-message-avatar">{message.role === 'user' ? t('You') : 'AI'}</div>
                <div className="ai-message-body">
                  <span className="ai-message-label">{message.role === 'user' ? t('You') : t('AI Assistant')}</span>
                  <div className="ai-message-bubble">
                    {message.role === 'assistant' && message.content
                      ? <ReportContent content={message.content} />
                      : (message.content || (message.isStreaming ? <Spinner size="sm" /> : ''))}
                  </div>
                  {message.isStreaming && <div role="status" className="small text-muted mt-2">
                    {message.status || t('Waiting for model response')}
                  </div>}
                  {message.role === 'assistant' && <ChatEvidence context={message.data_context} t={t} />}
                </div>
              </div>
            ))}
          </div>
        )}
        {activeConversation && activeTab === 'report' && <div className="ai-report-workspace">
          <div className="ai-report-configuration">
            <div className="ai-report-input-heading">
              <strong>{t('Generate report')}</strong>
              <span>{t('Complete the report information before generating.')}</span>
            </div>
            <Row form className="mt-3"><Col md={6}><Label for="ai-report-type">{t('Report type')}</Label><Input id="ai-report-type" type="select" value={reportType} disabled={isSending} onChange={event => setReportType(event.target.value)}><option value="">{t('Select a report type')}</option><option value="ghg_emissions">{t('GHG emissions report')}</option><option value="product_carbon_footprint">{t('Product carbon footprint report')}</option></Input></Col><Col md={6}><Label for="ai-report-mode">{t('Report mode')}</Label><Input id="ai-report-mode" type="select" value={reportMode} disabled={isSending || !reportType} onChange={event => setReportMode(event.target.value)}><option value="real_data">{t('Real data')}</option><option value="estimate_draft">{t('Estimate draft')}</option></Input></Col></Row>
            {reportInputOptions && <>
              {reportInputOptions.template && <div className="ai-template-summary">{t('Template in use')}: <strong>{reportInputOptions.template.name}</strong> · v{reportInputOptions.template.version_number}</div>}
              <div className="ai-report-data-note">{reportMode === 'real_data' ? t('Real-data reports use only authorized MyEMS data. Missing values are not fabricated.') : t('Estimate drafts must be reviewed before official use.')}</div>
              {reportType === 'ghg_emissions' && reportInputOptions.capabilities && reportInputOptions.capabilities.cost_analysis && <FormGroup check className="mt-3"><Label check><Input type="checkbox" checked={includeCostData} disabled={isSending} onChange={event => setIncludeCostData(event.target.checked)} /> {t('Include authorized cost and billing totals')}</Label></FormGroup>}
              <Row form className="mt-3"><Col md={6}><Label for="ai-report-period-start">{t('Report period start')} *</Label><Input id="ai-report-period-start" type="date" disabled={isSending} value={reportContext.period_start || ''} onChange={event => updateReportContext('period_start', event.target.value)} /></Col><Col md={6}><Label for="ai-report-period-end">{t('Report period end')} *</Label><Input id="ai-report-period-end" type="date" disabled={isSending} value={reportContext.period_end || ''} onChange={event => updateReportContext('period_end', event.target.value)} /></Col></Row>
              <Row form className="mt-3"><Col md={6}><Label for="ai-report-space">{t('Report enterprise')} *</Label><Input id="ai-report-space" type="select" disabled={isSending} value={reportContext.space_id || ''} onChange={event => updateReportContext('space_id', event.target.value)}><option value="">{t('Select report enterprise')}</option>{reportInputOptions.spaces.map(space => <option value={space.id} key={space.id}>{space.name}</option>)}</Input></Col><Col md={6}><Label for="ai-report-product">{t('Product')}{reportType === 'product_carbon_footprint' ? ' *' : ''}</Label><Input id="ai-report-product" type="select" disabled={isSending} value={reportContext.product_id || ''} onChange={event => updateReportContext('product_id', event.target.value)}><option value="">{t('No product selected')}</option>{reportInputOptions.products.filter(product => !reportContext.space_id || product.space_id === Number(reportContext.space_id)).map(product => <option value={product.id} key={product.id}>{product.name}</option>)}</Input></Col></Row>
              <FormGroup className="mt-3 mb-0"><Label for="ai-report-additional-information">{t('Additional information')}</Label><Input id="ai-report-additional-information" type="textarea" rows="2" disabled={isSending} value={reportContext.additional_information || ''} onChange={event => updateReportContext('additional_information', event.target.value)} /></FormGroup>
              <div className="ai-report-actions"><ButtonIcon icon="paperclip" color="outline-secondary" size="sm" title={t('Attachments')} aria-label={t('Attachments')} onClick={() => document.getElementById('ai-attachment-upload').click()} disabled={isSending} /><ButtonIcon icon={['far', 'paper-plane']} color="primary" size="sm" disabled={isSending} onClick={generateReport}>{isSending ? t('Generating') : t('Generate report')}</ButtonIcon></div>
            </>}
            <Input id="ai-attachment-upload" className="d-none" type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" disabled={isSending} onChange={uploadAttachment} />
            {attachments.length > 0 && <div className="ai-attachment-chips">{attachments.map(attachment => <Label check className="ai-attachment-chip mb-0" key={attachment.id}><Input type="checkbox" checked={selectedAttachmentIds.includes(attachment.id)} disabled={isSending || attachment.extraction_status !== 'completed'} onChange={() => toggleAttachment(attachment.id)} /> {attachment.filename}</Label>)}</div>}
          </div>
          {streamingReport && <div className="ai-report-streaming"><ReportStreaming content={streamingReport.content} reportType={streamingReport.type} status={streamingReport.status} t={t} /></div>}
          {error && lastReportRequest && !isSending && <div className="ai-report-retry"><Button color="outline-primary" size="sm" onClick={retryReport}>{t('Retry report')}</Button></div>}
          <div className="ai-report-history"><div className="ai-report-history-heading"><strong>{t('Reports in this conversation')}</strong></div>{reportVersions.length > 0 ? <div className="ai-report-history-list">{reportVersions.map(report => <ReportCard report={report} key={report.id} onPreview={setPreviewReport} onDownload={downloadReport} onRefine={beginRevision} t={t} />)}</div> : <div className="ai-report-empty">{t('No reports have been generated in this conversation.')}</div>}</div>
          {revisionTarget && <div className="ai-report-revision"><div className="ai-report-input-heading"><strong>{t('Continue refining')}</strong><span>{t('Based on version')} #{revisionTarget.id}</span></div><Input type="textarea" rows="3" value={revisionContent} disabled={isSending} onChange={event => setRevisionContent(event.target.value)} placeholder={t('Describe the changes you want in the full report')} /><div className="ai-report-actions"><Button color="link" size="sm" disabled={isSending} onClick={() => setRevisionTarget(null)}>{t('Cancel')}</Button><ButtonIcon icon={['far', 'paper-plane']} color="primary" size="sm" disabled={isSending || !revisionContent.trim()} onClick={submitRevision}>{isSending ? t('Generating') : t('Regenerate full report')}</ButtonIcon></div></div>}
        </div>}
        {activeConversation && activeTab === 'chat' && <div className="ai-composer-area">
          {useSystemData && <ChatClarification pending={chatPending} disabled={isSending} t={t}
            onSubmit={selection => sendMessage(chatPending.question, undefined, undefined, undefined, false,
              { ...chatPending.selection, ...selection })} />}
          <div className="ai-composer-toolbar">
            <FormGroup check className="mb-0">
              <Label check>
                <Input type="checkbox" disabled={isSending} checked={useSystemData} onChange={event => setUseSystemData(event.target.checked)} /> {t('Use system data')}
              </Label>
            </FormGroup>
            <ButtonIcon icon="paperclip" color="outline-secondary" size="sm" title={t('Attachments')} aria-label={t('Attachments')} onClick={() => document.getElementById('ai-chat-attachment-upload').click()} disabled={isSending} />
          </div>
          <Input id="ai-chat-attachment-upload" className="d-none" type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" disabled={isSending} onChange={uploadAttachment} />
          {attachments.length > 0 && <div className="ai-attachment-chips px-3">{attachments.map(attachment => <Label check className="ai-attachment-chip mb-0" key={attachment.id}><Input type="checkbox" checked={selectedAttachmentIds.includes(attachment.id)} disabled={isSending || attachment.extraction_status !== 'completed'} onChange={() => toggleAttachment(attachment.id)} /> {attachment.filename}</Label>)}</div>}
          <div className="ai-composer-input">
            <Input type="textarea" rows="2" value={content} disabled={!activeConversation || isSending} onChange={event => setContent(event.target.value)} onKeyDown={sendOnEnter} placeholder={t('Ask about energy and carbon data')} />
            <div className="ai-composer-footer"><span className="ai-composer-hint">{t('Press Enter to send')}</span><ButtonIcon icon={['far', 'paper-plane']} color="primary" size="sm" disabled={!activeConversation || isSending || !content.trim()} onClick={() => sendMessage()}>{isSending ? t('Generating') : t('Send')}</ButtonIcon></div>
          </div>
        </div>}
      </div>
      <Modal isOpen={Boolean(previewReport)} toggle={() => setPreviewReport(null)} size="xl" scrollable>
        <ModalHeader toggle={() => setPreviewReport(null)}>
          {previewReport && (previewReport.type === 'ghg_emissions' ? t('GHG emissions report') : t('Product carbon footprint report'))}
        </ModalHeader>
        <ModalBody className="bg-light">
          {previewReport && <div className="bg-white border p-4" style={{ minHeight: '70vh' }}>
            <ReportContent content={previewReport.content} />
          </div>}
        </ModalBody>
      </Modal>
    </div>
  );
};

export default withTranslation()(withRedirect(AIAssistant));
