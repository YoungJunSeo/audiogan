
import tensorflow as TF
import utiltf as util

class GAN(object):
    def __init__(self, args, d, g, z):
        super(GAN, self).__init__()

        self.buf_ph = TF.placeholder(TF.uint8, shape=(None, None, None, 3))
        self.buf_plot_op = TF.summary.image('waveform', self.buf_ph, max_outputs=args.batchsize)

class UnconditionalGAN(GAN):
    def __init__(self, args, d, g, z):
        super(UnconditionalGAN, self).__init__(args, d, g, z)
        x_real = TF.placeholder(TF.float32, shape=(None, args.amplitudes))
        x_real2 = TF.placeholder(TF.float32, shape=(None, args.amplitudes))
        lambda_ = TF.placeholder(TF.float32, shape=())

        x_fake = g.generate(batch_size=args.batchsize, length=args.amplitudes)
        comp, d_real, d_fake, pen, _, _ = d.compare(x_real, x_fake)
        comp_verify, d_verify_1, d_verify_2, pen_verify, _, _ = d.compare(x_real, x_real2)

        loss_d = comp + lambda_ * TF.reduce_mean(pen)
        metric_g = args.metric + '_g'
        if hasattr(util, metric_g):
            loss_g = TF.reduce_mean(getattr(util, metric_g)(d_fake))
        else:
            raise ValueError('not an eligible loss function')

        x = g.generate(z=z)

        d_summaries = [
                util.summarize_var(comp, 'comp', mean=True),
                util.summarize_var(d_real, 'd_real', mean=True),
                util.summarize_var(d_fake, 'd_fake', mean=True),
                util.summarize_var(pen, 'pen', mean=True, std=True),
                TF.summary.histogram('x_real', x_real),
                TF.summary.histogram('x_fake', x_fake),
                ]
        g_summaries = [
                util.summarize_var(d_fake, 'd_fake_g', mean=True),
                TF.summary.histogram('x_fake_g', x_fake),
                ]
        d_valid_summaries = d_summaries + [
                util.summarize_var(comp_verify, 'comp_verify', mean=True),
                util.summarize_var(d_verify_1, 'd_verify_1', mean=True),
                util.summarize_var(d_verify_2, 'd_verify_2', mean=True),
                util.summarize_var(d_verify_2 - d_verify_1, 'd_verify_diff', mean=True),
                util.summarize_var(pen_verify, 'pen_verify', mean=True, std=True),
                ]

        opt_g = TF.train.AdamOptimizer()
        opt_d = TF.train.AdamOptimizer()
        with TF.control_dependencies(TF.get_collection(TF.GraphKeys.UPDATE_OPS)):
            grad_g = opt_g.compute_gradients(loss_g, var_list=g.get_trainable_weights())
            grad_d = opt_d.compute_gradients(loss_d, var_list=d.get_trainable_weights())
        if args.ggradclip:
            pre_clipped_grad_g = [TF.norm(g_) for g_, v in grad_g if g_ is not None]
            grad_g = [(TF.clip_by_norm(_g, args.ggradclip), _v) for _g, _v in grad_g if _g is not None]
            g_summaries.append(
                util.summarize_var(pre_clipped_grad_g, 'pre_clipped_grad_g', min_=True, max_=True, mean=True, std=True)
                )
        if args.dgradclip:
            pre_clipped_grad_d = [TF.norm(g_) for g_, v in grad_d if g_ is not None]
            grad_d = [(TF.clip_by_norm(_g, args.dgradclip), _v) for _g, _v in grad_d if _g is not None]
            d_summaries.append(
                util.summarize_var(pre_clipped_grad_d, 'pre_clipped_grad_d', min_=True, max_=True, mean=True, std=True)
                )
        train_g = 0 if args.just_run == 'dis' else opt_g.apply_gradients(grad_g)
        train_d = 0 if args.just_run == 'gen' else opt_d.apply_gradients(grad_d)

        d_summaries = TF.summary.merge(d_summaries)
        d_valid_summaries = TF.summary.merge(d_valid_summaries)
        g_summaries = TF.summary.merge(g_summaries)
        audio_gen = TF.summary.audio('sample', x, 8000, max_outputs=args.batchsize)

        self.x_real = x_real
        self.x_real2 = x_real2
        self.x_fake = x_fake
        self.z = z
        self.x = x
        self.lambda_ = lambda_
        self.comp = comp
        self.loss_g = loss_g
        self.comp_verify = comp_verify
        self.d_verify_1 = d_verify_1
        self.d_verify_2 = d_verify_2
        self.train_d = train_d
        self.train_g = train_g

        self.d_summaries = d_summaries
        self.g_summaries = g_summaries
        self.d_valid_summaries = d_valid_summaries
        self.audio_gen = audio_gen

        self.d_real = d_real
        self.d_fake = d_fake
        self.pen = pen
