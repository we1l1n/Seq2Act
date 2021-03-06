"""Run tests on toy data for IRW models."""
import argparse
import cgi
import collections
import itertools
import json
import math
import numpy
import os
import random
import re
import sys
import theano

# Local imports
import atislexicon
import geolexicon
from augmentation import Augmenter
import domains
from attention import AttentionModel
from example import Example
import spec as specutil
from vocabulary import Vocabulary
from action_vocabulary import ActionVocabulary
from geoontology import GeoOntology
from generalontology import GeneralOntology
from atisontology import AtisOntology
from atisgeneralontology import AtisGeneralOntology
from geoaction2seq import action2seq as geo_action2seq
from atisaction2seq import action2seq as atis_action2seq


MODELS = collections.OrderedDict([
    ('attention', AttentionModel),
])

CONTROLLERS = collections.OrderedDict([
    ('geoontology', GeoOntology),
    ('generalontology', GeneralOntology),
    ('atisontology', AtisOntology),
    ('atisgeneralontology', AtisGeneralOntology)
])

CONVERTORS = collections.OrderedDict([
    ('geoaction2seq', geo_action2seq),
    ('atisaction2seq', atis_action2seq)
])

VOCAB_TYPES = collections.OrderedDict([
    ('raw', lambda s, e, **kwargs: Vocabulary.from_sentences(
        s, e, **kwargs)), 
    ('glove', lambda s, e, **kwargs: Vocabulary.from_sentences(
        s, e, use_glove=True, **kwargs)),
    ('action', lambda d, s, stru_em, sem_em, **kwargs: ActionVocabulary.from_databases(
        d, s, stru_em, sem_em, **kwargs)),
])

# Global options
OPTIONS = None

# Global statistics
STATS = {}

def _parse_args():
  global OPTIONS
  parser = argparse.ArgumentParser(
      description='A neural semantic parser.',
      formatter_class=argparse.RawTextHelpFormatter
  )
  parser.add_argument('--hidden-size', '-d', type=int,
                      help='Dimension of hidden units')
  parser.add_argument('--input-embedding-dim', '-i', type=int,
                      help='Dimension of input vectors.')
  parser.add_argument('--output-structure-embedding-dim', '-ost', type=int,
                      help='Dimension of output structure vectors.')
  parser.add_argument('--output-semantic-embedding-dim', '-ose', type=int,
                      help='Dimension of output semantic vectors.')
  parser.add_argument('--unk-cutoff', '-u', type=int, default=0,
                      help='Treat input words with <= this many occurrences as UNK.')
  parser.add_argument('--num-epochs', '-t', default=[],
                      type=lambda s: [int(x) for x in s.split(',')], 
                      help=('Number of epochs to train (default is no training).'
                            'If comma-separated list, will run for some epochs, halve learning rate, etc.'))
  parser.add_argument('--learning-rate', '-r', type=float, default=0.1,
                      help='Initial learning rate (default = 0.1).')
  parser.add_argument('--step-rule', '-s', default='simple',
                      help='Use a special SGD step size rule (types=[simple, adagrad, rmsprop,nesterov])')
  parser.add_argument('--lambda-reg', '-l', type=float, default=0.0,
                      help='L2 regularization constant (per example).')
  parser.add_argument('--rnn-type', '-c',
                      help='type of continuous RNN model (options: [%s])' % (
                          ', '.join(specutil.RNN_TYPES)))
  parser.add_argument('--model', '-m',
                      help='type of overall model (options: [%s])' % (
                          ', '.join(MODELS)))
  parser.add_argument('--domain-ontology', '-domo',
                      help='type of ontology (options: [%s])' % (
                        ', '.join(CONTROLLERS)))
  parser.add_argument('--general-ontology', '-geno',
                      help='type of ontology (options: [%s])' % (
                        ', '.join(CONTROLLERS)))
  parser.add_argument('--domain-convertor', '-conv',
                      help='type of convertor (options: [%s])' % (
                        ', '.join(CONVERTORS)))
  parser.add_argument('--input-vocab-type',
                      help='type of input vocabulary (options: [%s])' % (
                          ', '.join(VOCAB_TYPES)), default='raw')
  parser.add_argument('--output-vocab-type',
                      help='type of output vocabulary (options: [%s])' % (
                          ', '.join(VOCAB_TYPES)), default='action')
  parser.add_argument('--reverse-input', action='store_true',
                      help='Reverse the input sentence (intended for encoder-decoder).')
  parser.add_argument('--float32', action='store_true',
                      help='Use 32-bit floats (default is 64-bit/double precision).')
  parser.add_argument('--beam-size', '-k', type=int, default=0,
                      help='Use beam search with given beam size (default is greedy).')
  parser.add_argument('--domain', default=None,
                      help='Domain for augmentation and evaluation (options: [geoquery,atis,overnight-${domain}])')
  parser.add_argument('--augment', '-a',
                      help=('Options for augmentation.  Format: '
                            '"nesting+entity+concat2".'))
  parser.add_argument('--aug-frac', type=float, default=0.0,
                      help='How many recombinant examples to add, relative to '
                      'training set size.')
  parser.add_argument('--distract-prob', type=float, default=0.0,
                      help='Probability to introduce distractors during training.')
  parser.add_argument('--distract-num', type=int, default=0,
                      help='Number of distracting examples to use.')
  parser.add_argument('--concat-prob', type=float, default=0.0,
                      help='Probability to concatenate examples during training.')
  parser.add_argument('--concat-num', type=int, default=1,
                      help='Number of examples to concatenate together.')
  parser.add_argument('--domain-grammar', help='Path to grammar for domain.')
  parser.add_argument('--general-grammar', help='Path to grammar for general.')
  parser.add_argument('--train-data', help='Path to training data.')
  parser.add_argument('--dev-data', help='Path to dev data.')
  parser.add_argument('--dev-frac', type=float, default=0.0,
                      help='Take this fraction of train data as dev data.')
  parser.add_argument('--dev-seed', type=int, default=0,
                      help='RNG seed for the train/dev splits (default = 0)')
  parser.add_argument('--model-seed', type=int, default=0,
                      help="RNG seed for the model's initialization and SGD ordering (default = 0)")
  parser.add_argument('--save-file', help='Path to save parameters.')
  parser.add_argument('--load-file', help='Path to load parameters, will ignore other passed arguments.')
  parser.add_argument('--stats-file', help='Path to save statistics (JSON format).')
  parser.add_argument('--shell', action='store_true', 
                      help='Start an interactive shell.')
  parser.add_argument('--server', action='store_true', 
                      help='Start an interactive web console (requires bottle).')
  parser.add_argument('--hostname', default='127.0.0.1', help='server hostname')
  parser.add_argument('--port', default=9001, type=int, help='server port')
  parser.add_argument('--theano-fast-compile', action='store_true',
                      help='Run Theano in fast compile mode.')
  parser.add_argument('--theano-profile', action='store_true',
                      help='Turn on profiling in Theano.')
  parser.add_argument('--use-geoontology', '-usegeo', default=False,
                      help='use geo ontology for decoding.')
  parser.add_argument('--use-generalontology', '-usegen', default=False,
                      help='use general ontology for decoding.')
  parser.add_argument('--use-atisontology', '-useatis' , default=False,
                      help='use atis ontology for decoding.')
  parser.add_argument('--use-overnightontology', '-useover' , default=False,
                      help='use overnight ontology for decoding.')

  if len(sys.argv) == 1:
    parser.print_help()
    sys.exit(1)
  OPTIONS = parser.parse_args()
  
  # Some basic error checking
  if OPTIONS.rnn_type not in specutil.RNN_TYPES:
    print >> sys.stderr, 'Error: rnn type must be in %s' % (
        ', '.join(specutil.RNN_TYPES))
    sys.exit(1)
  if OPTIONS.model not in MODELS:
    print >> sys.stderr, 'Error: model must be in %s' % (
        ', '.join(MODELS))
    sys.exit(1)
  if OPTIONS.input_vocab_type not in VOCAB_TYPES:
    print >> sys.stderr, 'Error: input_vocab_type must be in %s' % (
        ', '.join(VOCAB_TYPES))
    sys.exit(1)
  if OPTIONS.output_vocab_type not in VOCAB_TYPES:
    print >> sys.stderr, 'Error: output_vocab_type must be in %s' % (
        ', '.join(VOCAB_TYPES))
    sys.exit(1)
  use_domain_ontology_count = 0
  if OPTIONS.use_geoontology:
      use_domain_ontology_count += 1
  if OPTIONS.use_atisontology:
      use_domain_ontology_count += 1
  if OPTIONS.use_overnightontology:
      use_domain_ontology_count += 1
  if use_domain_ontology_count > 1:
      print >> sys.stderr, 'Error: using most one domain ontology!'
      sys.exit(1)

def configure_theano():
  if OPTIONS.theano_fast_compile:
    theano.config.mode='FAST_COMPILE'
  else:
    theano.config.mode='FAST_RUN'
    theano.config.linker='cvm'
  if OPTIONS.theano_profile:
    theano.config.profile = True

def load_dataset(filename, domain):
  dataset = []
  with open(filename) as f:
    for line in f:
      x, y, z = line.rstrip('\n\r').split('\t')
      dataset.append((x, y, z))
  return dataset

def load_databases(filename, domain):
  databases = []
  with open(filename) as f:
    for line in f:
      databases.append(line)
  return databases

def get_input_vocabulary(dataset):
  sentences = [x[0] for x in dataset]
  constructor = VOCAB_TYPES[OPTIONS.input_vocab_type]
  if OPTIONS.float32:
    return constructor(sentences, OPTIONS.input_embedding_dim,
                       unk_cutoff=OPTIONS.unk_cutoff,
                       float_type=numpy.float32)
  else:
    return constructor(sentences, OPTIONS.input_embedding_dim,
                       unk_cutoff=OPTIONS.unk_cutoff)

def get_output_vocabulary(domain, databases):
  #sentences = [x[1] for x in dataset]
  constructor = VOCAB_TYPES[OPTIONS.output_vocab_type]
  if OPTIONS.float32:
    return constructor(domain, databases, OPTIONS.output_structure_embedding_dim, OPTIONS.output_semantic_embedding_dim,
                       float_type=numpy.float32)
  else:
    return constructor(domain, databases, OPTIONS.output_structure_embedding_dim, OPTIONS.output_semantic_embedding_dim)

def update_model(model, dataset):
  """Update model for new dataset if fixed word vectors were used.
  
  Note: glove_fixed has been removed for now.
  """
  need_new_model = False
  if OPTIONS.input_vocab_type == 'glove_fixed':
    in_vocabulary = get_input_vocabulary(dataset)
    need_new_model = True
  else:
    in_vocabulary = model.in_vocabulary

  if OPTIONS.output_vocab_type == 'glove_fixed':
    out_vocabulary = get_output_vocabulary(OPTIONS.domain, dataset)
    need_new_model = True
  else:
    out_vocabulary = model.out_vocabulary

  if need_new_model:
    spec = model.spec
    spec.set_in_vocabulary(in_vocabulary)
    spec.set_out_vocabulary(out_vocabulary)
    model = get_model(spec)  # Create a new model!
  return model

def preprocess_data(domain_convertor, domain_controller, general_controller, model, raw):
  in_vocabulary = model.in_vocabulary
  out_vocabulary = model.out_vocabulary

  data = []
  for raw_ex in raw:
    x_str, y_str, entity_lex_str = raw_ex
    entity_lex = {}
    if not entity_lex_str == '':
        entity_lex_items = entity_lex_str.split(' ')
        for entity_lex_item in entity_lex_items:
            parts = entity_lex_item.split(':::')
            entity = parts[0]
            entity_name = parts[1]
            entity_lex[entity] = entity_name
    y_toks = y_str.split(' ')
    y_new_toks = []
    #print('y_str: %s' % y_str)
    for y_tok in y_toks:
        if y_tok.startswith('add_entity_node:'):
            entity = y_tok[y_tok.index(':-:') + 3:]
            if entity in entity_lex:
                new_entity = entity_lex[entity]
                y_new_tok = y_tok.replace(entity, new_entity)
                y_new_toks.append(y_new_tok)
                continue
        y_new_toks.append(y_tok)
    y_new_str = ' '.join(y_new_toks)
    #print('y_new_str: %s' % y_new_str)
    #print('entity_lex: %s' % entity_lex)
    y_str_lf = ' '.join(domain_convertor(y_new_str, domain_controller, general_controller, entity_lex_map=entity_lex))
    #print('y_str_ls: %s' % y_str_lf)
    ex = Example(x_str, y_str, y_str_lf, entity_lex, in_vocabulary, out_vocabulary,
                 reverse_input=OPTIONS.reverse_input)
    data.append(ex)
  return data

def get_spec(in_vocabulary, out_vocabulary):
  kwargs = {'rnn_type': OPTIONS.rnn_type, 'step_rule': OPTIONS.step_rule}
  constructor = MODELS[OPTIONS.model].get_spec_class()
  return constructor(in_vocabulary, out_vocabulary,
                     OPTIONS.hidden_size, **kwargs)

def get_model(spec):
  constructor = MODELS[OPTIONS.model]
  if OPTIONS.float32:
    model = constructor(spec, distract_num=OPTIONS.distract_num, float_type=numpy.float32)
  else:
    model = constructor(spec, distract_num=OPTIONS.distract_num)
  return model

def print_accuracy_metrics(name, is_correct_list, tokens_correct_list,
                           x_len_list, y_len_list, denotation_correct_list):
  # Overall metrics
  num_examples = len(is_correct_list)
  num_correct = sum(is_correct_list)
  num_tokens_correct = sum(tokens_correct_list)
  num_tokens = sum(y_len_list)
  seq_accuracy = float(num_correct) / num_examples
  token_accuracy = float(num_tokens_correct) / num_tokens

  STATS[name] = {}

  # Print sequence-level accuracy
  STATS[name]['sentence'] = {
      'correct': num_correct,
      'total': num_examples,
      'accuracy': seq_accuracy,
  }
  print 'Sequence-level accuracy: %d/%d = %g' % (num_correct, num_examples, seq_accuracy)

  # Print token-level accuracy
  STATS[name]['token'] = {
      'correct': num_tokens_correct,
      'total': num_tokens,
      'accuracy': token_accuracy,
  }
  print 'Token-level accuracy: %d/%d = %g' % (num_tokens_correct, num_tokens, token_accuracy)

  # Print denotation-level accuracy
  if denotation_correct_list:
    denotation_correct = sum(denotation_correct_list)
    denotation_accuracy = float(denotation_correct)/num_examples
    STATS[name]['denotation'] = {
        'correct': denotation_correct,
        'total': num_examples,
        'accuracy': denotation_accuracy
    }
    print 'Denotation-level accuracy: %d/%d = %g' % (denotation_correct, num_examples, denotation_accuracy)

def decode(model, ex, domain_convertor, domain_controller, general_controller):
  if OPTIONS.beam_size == 0:
    return model.decode_greedy(OPTIONS.domain, ex, domain_convertor, domain_controller, general_controller, max_len=100)
  else:
    return model.decode_beam(OPTIONS.domain, ex, domain_convertor, domain_controller, general_controller, beam_size=OPTIONS.beam_size)

def evaluate(name, model, domain_convertor, domain_controller, general_controller, dataset, domain=None):
  """Evaluate the model. """
  in_vocabulary = model.in_vocabulary
  out_vocabulary = model.out_vocabulary

  is_correct_list = []
  tokens_correct_list = []
  x_len_list = []
  y_len_list = []

  if domain:
    all_derivs = [decode(model, ex, domain_convertor, domain_controller, general_controller) for ex in dataset]
    true_answers = [ex.y_str for ex in dataset]
    true_answers_lf = [ex.y_str_lf for ex in dataset]
    derivs, denotation_correct_list = domain.compare_answers(true_answers, true_answers_lf, all_derivs)
  else:
    derivs = [decode(model, ex, domain_convertor, domain_controller, general_controller)[0] for ex in dataset]
    denotation_correct_list = None

  print('derivs size: ', len(derivs))
  print('denotation_correct_list: ', len(denotation_correct_list))
  print('dataset size: ', len(dataset))

  for i, ex in enumerate(dataset):
    print 'Example %d' % i
    print '  x         = "%s"' % ex.x_str
    print '  y         = "%s"' % ex.y_str
    if derivs[i] == None:
        y_pred_toks = []
        y_pred_str = ''
        y_pred_str_lf = ''
        p_list = []
        entity_lex = {}
    else:
        y_pred_toks = derivs[i].y_toks
        y_pred_str = ' '.join(y_pred_toks)
        y_pred_toks_lf = derivs[i].y_toks_lf
        y_pred_str_lf = ' '.join(y_pred_toks_lf)
        p_list = derivs[i].p_list
        entity_lex = derivs[i].entity_lex_map

    # Compute accuracy metrics
    is_correct = (y_pred_str == ex.y_str)
    tokens_correct = sum(a == b for a, b in zip(y_pred_toks, ex.y_toks))
    is_correct_list.append(is_correct)
    tokens_correct_list.append(tokens_correct)
    x_len_list.append(len(ex.x_toks))
    y_len_list.append(len(ex.y_toks))
    y_str_for_query = ex.y_str_lf.replace('_', '').replace(' ', '')
    y_pred_str_for_query = y_pred_str_lf.replace('_', '').replace(' ', '')

    print('  y_pred (len = %s)    = "%s"' % (str(len(y_pred_str.split(' '))), y_pred_str))
    print('  p_list (len = %s)    = ' % (str(len(p_list))) , p_list)
    print('  y_lf      = "%s"' % ex.y_str_lf)
    print('  y_pred_lf = "%s"' % y_pred_str_lf)
    #print('  y_str_for_query = "%s"' % y_str_for_query)
    #print('  y_pred_str_for_query = "%s"' % y_pred_str_for_query)
    print('  entity_lex = "%s"' % entity_lex)
    print('  sequence correct = %s' % is_correct)
    print('  token accuracy = %d/%d = %g' % (
        tokens_correct, len(ex.y_toks), float(tokens_correct) / len(ex.y_toks)))
    if denotation_correct_list:
      denotation_correct = denotation_correct_list[i]
      print('  denotation correct = %s' % denotation_correct)
  print_accuracy_metrics(name, is_correct_list, tokens_correct_list,
                         x_len_list, y_len_list, denotation_correct_list)

def run_shell(model):
  print('==== Neural Network Semantic Parsing REPL ====')
  print('')
  print('Enter an utterance:')
  while True:
    s = raw_input('> ').strip()
    example = Example(s, '', '', {}, model.in_vocabulary, model.out_vocabulary, reverse_input=OPTIONS.reverse_input)
    print('')
    print('Result:')
    preds = decode(model, example)
    for prob, y_toks in preds[:10]:
      y_str = ' '.join(y_toks)
      print('  [p=%f] %s' % (prob, y_str))
    print('')

def make_heatmap(x_str, y_str, attention_list, copy_list):
  """Make an HTML heatmap of attention."""
  def css_color(r, g, b):
    """r, g, b are in 0-1, make """
    r2 = int(r * 255)
    g2 = int(g * 255)
    b2 = int(b * 255)
    return 'rgb(%d,%d,%d)' % (r2, g2, b2)

  x_toks = [cgi.escape(w) for w in x_str.split(' ')] + ['EOS']
  if y_str == '':
    y_toks = ['EOS']
  else:
    y_toks = [cgi.escape(w) for w in y_str.split(' ')] + ['EOS']
  lines = ['<table>', '<tr>', '<td/>']
  for w in y_toks:
    lines.append('<td>%s</td>' % w)
  lines.append('</tr>')
  for i, w in enumerate(x_toks):
    lines.append('<tr>')
    lines.append('<td>%s</td>' % w)
    for j in range(len(y_toks)):
      do_copy = copy_list[j]
      if do_copy:
        color = css_color(1 - attention_list[j][i], 1 - attention_list[j][i], 1)
      else:
        color = css_color(1, 1 - attention_list[j][i], 1 - attention_list[j][i])
      lines.append('<td/ style="background-color: %s">' % color)
    lines.append('</tr>')
  lines.append('</table>')
  return '\n'.join(lines)

def run_server(model, hostname='127.0.0.1', port=9001):
  import bottle
  print '==== Neural Network Semantic Parsing Server ===='

  app = bottle.Bottle()
  
  @app.route('/debug')
  def debug():
    content = make_heatmap(
        'what states border texas',
        'answer ( A , ( state ( A ) , next_to ( A , B ) , const ( B , stateid ( texas ) ) ) )',
        [[0.0, 0.25, 0.5, 0.75, 1.0]] * 29)
    return bottle.template('main', prompt='Enter a new query', content=content)

  @app.route('/post_query')
  def post_query():
    query = bottle.request.params.get('query')
    print 'Received query: "%s"' % query
    example = Example(query, '', '', {}, model.in_vocabulary, model.out_vocabulary, reverse_input=OPTIONS.reverse_input)
    preds = decode(model, example)
    lines = ['<b>Query: "%s"</b>' % query, '<ul>']
    for i, deriv in enumerate(preds[:10]):
      y_str = ' '.join(deriv.y_toks)
      lines.append('<li> %d. [p=%f] %s' % (i, deriv.p, y_str))
      lines.append(make_heatmap(query, y_str, deriv.attention_list, deriv.copy_list))
    lines.append('</ul>')

    content = '\n'.join(lines)
    return bottle.template('main', prompt='Enter a new query', content=content)

  @app.route('/')
  def index():
    return bottle.template('main', prompt='Enter a query', content='')

  bottle.run(app, host=hostname, port=port)

def load_raw_all(domain=None):
  # Load train, and dev too if dev-frac was provided
  random.seed(OPTIONS.dev_seed)
  if OPTIONS.train_data:
    train_raw = load_dataset(OPTIONS.train_data, domain=domain)
    if OPTIONS.dev_frac > 0.0:
      num_dev = int(round(len(train_raw) * OPTIONS.dev_frac))
      random.shuffle(train_raw)
      dev_raw = train_raw[:num_dev]
      train_raw = train_raw[num_dev:]
      print >> sys.stderr, 'Split dataset into %d train, %d dev examples' % (
          len(train_raw), len(dev_raw))
    else:
      dev_raw = None
  else:
    train_raw = None
    dev_raw = None

  # Load dev data from separate file
  if OPTIONS.dev_data:
    if dev_raw:
      # Overwrite dev frac from before, if it existed
      print >> sys.stderr, 'WARNING: Replacing dev-frac dev data with dev-data'
    dev_raw = load_dataset(OPTIONS.dev_data, domain=domain)

  return train_raw, dev_raw

def get_augmenter(train_raw, domain):
  if OPTIONS.augment:
    aug_types = OPTIONS.augment.split('+')
    augmenter = Augmenter(domain, train_raw, aug_types)
    return augmenter
  else:
    return None


def get_lexicon():
  if OPTIONS.use_lexicon:
    if OPTIONS.domain == 'atis':
      return atislexicon.get_lexicon()
    elif OPTIONS.domain == 'geoquery':
      return geolexicon.get_lexicon()
    raise Exception('No lexicon for domain %s' % OPTIONS.domain)
  return None

def init_spec(train_raw, databases):
  if OPTIONS.load_file:
    print >> sys.stderr, 'Loading saved params from %s' % OPTIONS.load_file
    spec = specutil.load(OPTIONS.load_file)
  elif OPTIONS.train_data:
    print >> sys.stderr, 'Initializing parameters...'
    in_vocabulary = get_input_vocabulary(train_raw)
    out_vocabulary = get_output_vocabulary(OPTIONS.domain, databases)
    #out_vocabulary = get_output_vocabulary(train_raw)
    spec = get_spec(in_vocabulary, out_vocabulary)
  else:
    raise Exception('Must either provide parameters to load or training data.')
  return spec

def evaluate_train(model, domain_convertor, domain_controller, general_controller, train_data, domain=None):
  print >> sys.stderr, 'Evaluating on training data...'
  print 'Training data:'
  evaluate('train', model, domain_convertor, domain_controller, general_controller, train_data, domain=domain)

def evaluate_dev(model, domain_convertor, domain_controller, general_controller, dev_raw, domain=None):
  print >> sys.stderr, 'Evaluating on dev data...'
  dev_model = update_model(model, dev_raw)
  dev_data = preprocess_data(domain_convertor, domain_controller, general_controller, dev_model, dev_raw)
  print 'Dev data:'
  evaluate('dev', dev_model, domain_convertor, domain_controller, general_controller, dev_data, domain=domain)

def write_stats():
  if OPTIONS.stats_file:
    out = open(OPTIONS.stats_file, 'w')
    print >>out, json.dumps(STATS)
    out.close()

def run():
  configure_theano()
  domain = None
  if OPTIONS.domain:
    domain = domains.new(OPTIONS.domain)
  train_raw, dev_raw = load_raw_all(domain=domain)
  databases = load_databases(OPTIONS.domain_grammar, domain=domain)
  random.seed(OPTIONS.model_seed)
  numpy.random.seed(OPTIONS.model_seed)
  spec = init_spec(train_raw, databases)
  model = get_model(spec)
  constructor1 = CONTROLLERS[OPTIONS.general_ontology]
  constructor2 = CONTROLLERS[OPTIONS.domain_ontology]
  use_general_ontology = OPTIONS.use_generalontology
  use_domain_ontology = OPTIONS.use_geoontology or OPTIONS.use_atisontology or OPTIONS.use_overnightontology
  general_controller = constructor1(OPTIONS.general_grammar, use_general_ontology)
  domain_controller = constructor2(OPTIONS.domain_grammar, use_domain_ontology)
  constructor3 = CONVERTORS[OPTIONS.domain_convertor]
  domain_convertor = constructor3



  if train_raw:
    train_data = preprocess_data(domain_convertor, domain_controller, general_controller, model, train_raw)
    random.seed(OPTIONS.model_seed)
    dev_data = None
    if dev_raw:
      dev_data = preprocess_data(domain_convertor, domain_controller, general_controller, model, dev_raw)
    augmenter = get_augmenter(train_raw, domain)
    model.train(train_data, T=OPTIONS.num_epochs, eta=OPTIONS.learning_rate,
                dev_data=dev_data, l2_reg=OPTIONS.lambda_reg,
                distract_prob=OPTIONS.distract_prob,
                distract_num=OPTIONS.distract_num,
                concat_prob=OPTIONS.concat_prob, concat_num=OPTIONS.concat_num,
                augmenter=augmenter, aug_frac=OPTIONS.aug_frac)

  if OPTIONS.save_file:
    print >> sys.stderr, 'Saving parameters...'
    spec.save(OPTIONS.save_file)


  evaluation_train_flag = False
  if train_raw and evaluation_train_flag:
    evaluate_train(model, domain_convertor, domain_controller, general_controller, train_data, domain=domain)
  if dev_raw:
    evaluate_dev(model, domain_convertor, domain_controller, general_controller, dev_raw, domain=domain)

  write_stats()

  if OPTIONS.shell:
    run_shell(model)
  elif OPTIONS.server:
    run_server(model, hostname=OPTIONS.hostname, port=OPTIONS.port)

def main():
  _parse_args()
  print OPTIONS
  print >> sys.stderr, OPTIONS
  run()

if __name__ == '__main__':
  main()
