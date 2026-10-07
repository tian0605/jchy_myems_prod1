import React, { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  Col,
  FormGroup,
  Input,
  Label,
  ListGroup,
  ListGroupItem,
  Row,
  Spinner
} from 'reactstrap';
import { withTranslation } from 'react-i18next';
import withRedirect from '../../../hoc/withRedirect';
import { APIBaseURL, settings } from '../../../config';
import { createCookie, getCookieValue } from '../../../helpers/utils';

const AIAssistant = ({ setRedirect, setRedirectUrl, t }) => {
  const [conversations, setConversations] = useState([]);
  const [activeConversation, setActiveConversation] = useState(null);
  const [models, setModels] = useState([]);
  const [content, setContent] = useState('');
  const [useSystemData, setUseSystemData] = useState(true);
  const [isLoading, setIsLoading] = useState(true);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState('');

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
    setError('');
    return fetch(APIBaseURL + '/ai-assistant/conversations/' + conversationId, requestOptions('GET'))
      .then(response => {
        if (!response.ok) {
          throw new Error(t('Unable to load conversation'));
        }
        return response.json();
      })
      .then(json => setActiveConversation(json))
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

  const createConversation = () => {
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

  const sendMessage = () => {
    const message = content.trim();
    if (!message || !activeConversation || isSending) {
      return;
    }
    setContent('');
    setError('');
    setIsSending(true);
    const optimisticUserMessage = { id: 'local-user', role: 'user', content: message };
    const optimisticAssistantMessage = { id: 'local-assistant', role: 'assistant', content: '', isStreaming: true };
    setActiveConversation(previous => ({
      ...previous,
      messages: previous.messages.concat([optimisticUserMessage, optimisticAssistantMessage])
    }));

    fetch(APIBaseURL + '/ai-assistant/conversations/' + activeConversation.id + '/messages', {
      ...requestOptions('POST'),
      body: JSON.stringify({ content: message, use_system_data: useSystemData })
    })
      .then(response => {
        if (!response.ok || !response.body) {
          throw new Error(t('Unable to send message'));
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        const readNext = () =>
          reader.read().then(({ done, value }) => {
            if (done) {
              return null;
            }
            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split('\n\n');
            buffer = events.pop();
            events.forEach(rawEvent => {
              const eventName = (rawEvent.match(/^event: (.+)$/m) || [])[1];
              const dataLine = (rawEvent.match(/^data: (.+)$/m) || [])[1];
              if (!dataLine) {
                return;
              }
              const data = JSON.parse(dataLine);
              if (eventName === 'delta') {
                appendAssistantDelta(data.content);
              } else if (eventName === 'error') {
                setError(data.message);
              }
            });
            return readNext();
          });
        return readNext();
      })
      .then(() => loadConversation(activeConversation.id))
      .then(loadConversations)
      .catch(sendError => setError(sendError.message))
      .finally(() => setIsSending(false));
  };

  if (isLoading) {
    return <Spinner color="primary" />;
  }

  return (
    <Row>
      <Col lg={3} className="mb-3">
        <Card className="h-100">
          <CardHeader className="d-flex justify-content-between align-items-center">
            <span>{t('AI Assistant')}</span>
            <Button color="primary" size="sm" onClick={createConversation}>
              +
            </Button>
          </CardHeader>
          <ListGroup flush>
            {conversations.map(conversation => (
              <ListGroupItem
                action
                active={activeConversation && activeConversation.id === conversation.id}
                key={conversation.id}
                onClick={() => loadConversation(conversation.id)}
              >
                {conversation.title}
              </ListGroupItem>
            ))}
          </ListGroup>
        </Card>
      </Col>
      <Col lg={9}>
        <Card>
          <CardHeader className="d-flex justify-content-between align-items-center">
            <span>{activeConversation ? activeConversation.title : t('New conversation')}</span>
            {models.length > 0 && <Badge color="info">{models[0].name}</Badge>}
          </CardHeader>
          <CardBody>
            {error && <Alert color="danger">{error}</Alert>}
            {!activeConversation && <Alert color="info">{t('Create a conversation to start')}</Alert>}
            {activeConversation && (
              <div className="mb-4" style={{ minHeight: '360px', maxHeight: '60vh', overflowY: 'auto' }}>
                {activeConversation.messages.map((message, index) => (
                  <div className={'mb-3 ' + (message.role === 'user' ? 'text-right' : '')} key={message.id || index}>
                    <Badge color={message.role === 'user' ? 'primary' : 'secondary'} className="mr-2">
                      {message.role === 'user' ? t('You') : t('AI Assistant')}
                    </Badge>
                    <div
                      className="d-inline-block text-left border rounded p-2 mw-75"
                      style={{ whiteSpace: 'pre-wrap' }}
                    >
                      {message.content || (message.isStreaming ? <Spinner size="sm" /> : '')}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <FormGroup check className="mb-2">
              <Label check>
                <Input
                  type="checkbox"
                  checked={useSystemData}
                  onChange={event => setUseSystemData(event.target.checked)}
                />{' '}
                {t('Use system data')}
              </Label>
            </FormGroup>
            <Input
              type="textarea"
              rows="3"
              value={content}
              disabled={!activeConversation || isSending}
              onChange={event => setContent(event.target.value)}
              placeholder={t('Ask about energy and carbon data')}
            />
            <Button
              color="primary"
              className="mt-2"
              disabled={!activeConversation || isSending || !content.trim()}
              onClick={sendMessage}
            >
              {isSending ? t('Generating') : t('Send')}
            </Button>
          </CardBody>
        </Card>
      </Col>
    </Row>
  );
};

export default withTranslation()(withRedirect(AIAssistant));
